const { chromium } = require("playwright-core");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { analyzePersonaOutcome, decideReaction, summarizeReactions } = require("./playwright_persona_reaction_engine");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8100";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PYTHON = process.env.PYTHON_BIN || path.join(".venv", "Scripts", "python.exe");
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `persona-policy-lab-${RUN_ID}`);
const PROFILES_PATH = path.join("tools", "persona_policy_profiles.json");

fs.mkdirSync(OUT_DIR, { recursive: true });

const profiles = JSON.parse(fs.readFileSync(PROFILES_PATH, "utf8"));
const profileById = Object.fromEntries(profiles.map((profile) => [profile.id, profile]));
const results = [];

function repeat(value, count) {
  return Array.from({ length: count }, () => value);
}

function scenarioDays(patternCodes) {
  return patternCodes.map((pattern, index) => ({ day: index + 1, pattern }));
}

const personaScenarios = [
  {
    personaId: "lazy_warning_responder",
    title: "게으른 사용자: 설득/실용 실패 후 warning_soft에서 복용",
    days: scenarioDays(repeat("B", 7)),
    expectedTones: ["persuasion", "persuasion", "practical", "practical", "warning_soft", "warning_soft", "warning_soft"],
    expectedActions: ["ignore", "ignore", "snooze", "snooze", "take", "take", "take"],
    expectedRecommendation: {
      recommendedNextTone: "warning_soft",
      strategy: "direct_adherence",
      avoidTones: ["persuasion", "practical"],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === 5, `lazy first take day mismatch:${JSON.stringify(summary)}`);
      assert(summary.byTone.warning_soft && summary.byTone.warning_soft.success === 3, `lazy warning success missing:${JSON.stringify(summary)}`);
    },
  },
  {
    personaId: "busy_practical_responder",
    title: "바쁜 사용자: practical 전환 후 빠른 복용",
    days: scenarioDays(repeat("B", 7)),
    expectedTones: ["persuasion", "persuasion", "practical", "practical", "practical", "practical", "practical"],
    expectedActions: ["snooze", "snooze", "take", "take", "take", "take", "take"],
    expectedRecommendation: {
      recommendedNextTone: "practical",
      strategy: "direct_adherence",
      avoidTones: ["persuasion"],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === 3, `busy first take day mismatch:${JSON.stringify(summary)}`);
      assert(!summary.byTone.warning_soft, `busy persona escalated to warning unexpectedly:${JSON.stringify(summary)}`);
    },
  },
  {
    personaId: "anxious_support_responder",
    title: "불안 사용자: 지지형에는 답하지만 warning_soft에서는 opt-out",
    days: scenarioDays([...repeat("B", 6), "D"]),
    expectedTones: ["persuasion", "persuasion", "persuasion", "persuasion", "persuasion", "persuasion", "warning_soft"],
    expectedActions: ["reply", "reply", "reply", "reply", "reply", "reply", "suppress"],
    expectedRecommendation: {
      recommendedNextTone: "persuasion",
      strategy: "support_first",
      avoidTones: ["warning_soft"],
    },
    assertScenario: (summary) => {
      assert(summary.actions.includes("suppress"), `anxious suppression action missing:${JSON.stringify(summary)}`);
      assert(summary.suppressedFollowup && summary.suppressedFollowup.missed_payload_count === 0, `suppression guard failed:${JSON.stringify(summary)}`);
      assert(
        summary.suppressedFollowup.missed_dose_message_count === summary.suppressedFollowup.missed_dose_message_count_before_followup,
        `suppression created missed-dose chat:${JSON.stringify(summary.suppressedFollowup)}`,
      );
    },
  },
  {
    personaId: "routine_empathy_responder",
    title: "루틴형 사용자: A 패턴 공감형에서 즉시 복용 전환",
    days: scenarioDays(repeat("A", 7)),
    expectedTones: ["empathy", "empathy", "empathy", "empathy", "empathy", "empathy", "empathy"],
    expectedActions: ["take", "take", "take", "take", "take", "take", "take"],
    expectedRecommendation: {
      recommendedNextTone: "empathy",
      strategy: "direct_adherence",
      avoidTones: [],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === 1, `routine first take day mismatch:${JSON.stringify(summary)}`);
      assert(summary.byTone.empathy && summary.byTone.empathy.success === 7, `routine empathy success missing:${JSON.stringify(summary)}`);
    },
  },
  {
    personaId: "data_persuasion_responder",
    title: "설득 반응 사용자: B 패턴 설득형에서 바로 복용 전환",
    days: scenarioDays(repeat("B", 7)),
    expectedTones: ["persuasion", "persuasion", "persuasion", "persuasion", "persuasion", "persuasion", "persuasion"],
    expectedActions: ["take", "take", "take", "take", "take", "take", "take"],
    expectedRecommendation: {
      recommendedNextTone: "persuasion",
      strategy: "direct_adherence",
      avoidTones: [],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === 1, `data persuasion first take day mismatch:${JSON.stringify(summary)}`);
      assert(summary.byTone.persuasion && summary.byTone.persuasion.success === 7, `persuasion success missing:${JSON.stringify(summary)}`);
    },
  },
  {
    personaId: "side_effect_reporter",
    title: "부작용 대화형 사용자: C 패턴은 항상 side_effect_check 우선",
    days: scenarioDays(repeat("C", 7)),
    expectedTones: [
      "side_effect_check",
      "side_effect_check",
      "side_effect_check",
      "side_effect_check",
      "side_effect_check",
      "side_effect_check",
      "side_effect_check",
    ],
    expectedActions: ["reply", "reply", "reply", "reply", "reply", "reply", "reply"],
    expectedRecommendation: {
      recommendedNextTone: "side_effect_check",
      strategy: "conversation_first",
      avoidTones: [],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === null, `side effect persona should not take:${JSON.stringify(summary)}`);
      assert(summary.byTone.side_effect_check && summary.byTone.side_effect_check.success === 7, `side effect replies missing:${JSON.stringify(summary)}`);
    },
  },
  {
    personaId: "warning_avoidant_conversationalist",
    title: "경고 회피 대화형 사용자: warning_soft에서는 복용보다 답변",
    days: scenarioDays(["B", "B", "B", "B", "D", "D", "D"]),
    expectedTones: ["persuasion", "persuasion", "practical", "practical", "warning_soft", "warning_soft", "warning_soft"],
    expectedActions: ["ignore", "ignore", "snooze", "snooze", "reply", "reply", "reply"],
    expectedRecommendation: {
      recommendedNextTone: "warning_soft",
      strategy: "conversation_first",
      avoidTones: ["persuasion", "practical"],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === null, `warning avoidant should not take:${JSON.stringify(summary)}`);
      assert(summary.byTone.warning_soft && summary.byTone.warning_soft.actions.reply === 3, `warning replies missing:${JSON.stringify(summary)}`);
    },
  },
  {
    personaId: "support_conversation_responder",
    title: "대화 우선 사용자: 설득형에서 복용 전 대화 참여",
    days: scenarioDays(repeat("B", 7)),
    expectedTones: ["persuasion", "persuasion", "persuasion", "persuasion", "persuasion", "persuasion", "persuasion"],
    expectedActions: ["reply", "reply", "reply", "reply", "reply", "reply", "reply"],
    expectedRecommendation: {
      recommendedNextTone: "persuasion",
      strategy: "conversation_first",
      avoidTones: [],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === null, `support conversation persona should not take:${JSON.stringify(summary)}`);
      assert(summary.recommendation.first_reply_day === 1, `support first reply mismatch:${JSON.stringify(summary.recommendation)}`);
    },
  },
  {
    personaId: "unresponsive_explore_target",
    title: "무반응 사용자: 모든 기본 escalation 실패 후 practical 탐색 권고",
    days: scenarioDays(repeat("B", 7)),
    expectedTones: ["persuasion", "persuasion", "practical", "practical", "warning_soft", "warning_soft", "warning_soft"],
    expectedActions: ["ignore", "ignore", "ignore", "ignore", "ignore", "ignore", "ignore"],
    expectedRecommendation: {
      recommendedNextTone: "practical",
      strategy: "explore_practical",
      avoidTones: ["persuasion", "practical", "warning_soft"],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === null, `unresponsive persona should not take:${JSON.stringify(summary)}`);
      assert(summary.recommendation.take_count === 0 && summary.recommendation.reply_count === 0, `unresponsive success counted:${JSON.stringify(summary.recommendation)}`);
    },
  },
  {
    personaId: "low_adherence_warning_responder",
    title: "장기 저조 사용자: E 패턴 warning_soft에서 복용 전환",
    days: scenarioDays(repeat("E", 7)),
    expectedTones: ["warning_soft", "warning_soft", "warning_soft", "warning_soft", "warning_soft", "warning_soft", "warning_soft"],
    expectedActions: ["take", "take", "take", "take", "take", "take", "take"],
    expectedRecommendation: {
      recommendedNextTone: "warning_soft",
      strategy: "direct_adherence",
      avoidTones: [],
    },
    assertScenario: (summary) => {
      assert(summary.firstTakeDay === 1, `low adherence first take day mismatch:${JSON.stringify(summary)}`);
      assert(summary.byTone.warning_soft && summary.byTone.warning_soft.success === 7, `low adherence warning success missing:${JSON.stringify(summary)}`);
    },
  },
];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function assertRecommendation(summary, expected) {
  if (!expected) {
    return;
  }
  const recommendation = summary.recommendation || {};
  assert(
    recommendation.recommended_next_tone === expected.recommendedNextTone,
    `recommendation tone mismatch:${JSON.stringify(recommendation)} expected=${expected.recommendedNextTone}`,
  );
  assert(
    recommendation.strategy === expected.strategy,
    `recommendation strategy mismatch:${JSON.stringify(recommendation)} expected=${expected.strategy}`,
  );
  for (const toneKey of expected.avoidTones || []) {
    assert(
      (recommendation.avoid_tones || []).includes(toneKey),
      `recommendation avoid tone missing:${toneKey}:${JSON.stringify(recommendation)}`,
    );
  }
}

function fixture(command, options = {}) {
  const args = ["tools/playwright_adherence_pattern_fixture.py", command];
  if (options.scenario) {
    args.push(options.scenario);
  }
  for (const [key, value] of Object.entries(options.flags || {})) {
    args.push(`--${key}`);
    args.push(String(value));
  }
  const result = spawnSync(PYTHON, args, {
    cwd: process.cwd(),
    encoding: "utf8",
    env: process.env,
  });
  if (result.status !== 0) {
    throw new Error(`fixture_${command}_failed:${result.stderr || result.stdout}`);
  }
  const stdout = (result.stdout || "").trim();
  return stdout ? JSON.parse(stdout) : {};
}

async function getJson(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function postForm(pathname, values = {}) {
  const response = await fetch(`${BASE_URL}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(values),
  });
  if (!response.ok && response.status !== 204) {
    throw new Error(`POST ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.text();
}

async function performReaction(page, seeded, reaction) {
  if (reaction.action === "ignore" || reaction.action === "snooze") {
    await page.waitForTimeout(150);
    return;
  }
  if (reaction.action === "take" && seeded.related_dose_event_id) {
    await postForm(`/doses/${seeded.related_dose_event_id}/take`);
    return;
  }
  if (reaction.action === "suppress") {
    await postForm("/reminders/suppression", { suppressed: "true" });
    return;
  }
  if (reaction.action === "reply") {
    await page.waitForTimeout(150);
    return;
  }
}

async function runPersonaScenario(page, scenario) {
  const started = Date.now();
  const persona = profileById[scenario.personaId];
  const screenshots = [];
  if (!persona) {
    throw new Error(`persona_not_found:${scenario.personaId}`);
  }
  try {
    fixture("persona-reset");
    const dayResults = [];
    const reactions = [];
    for (const daySpec of scenario.days) {
      const seeded = fixture("persona-seed-round", {
        flags: {
          pattern: daySpec.pattern,
          "simulation-day": daySpec.day,
        },
      });
      await page.goto(`${BASE_URL}/?persona=${encodeURIComponent(persona.id)}&day=${daySpec.day}#chat`, {
        waitUntil: "domcontentloaded",
        timeout: 20000,
      });
      await page.waitForSelector("#chat-panel", { timeout: 10000 });
      const panelText = await page.locator("#chat-panel").innerText();
      assert(panelText.includes(seeded.chat_content), `${persona.id} day ${daySpec.day} chat message not visible:${panelText}`);
      assert(seeded.tone_policy && seeded.tone_policy.tone_key, `${persona.id} day ${daySpec.day} tone metadata missing:${JSON.stringify(seeded)}`);
      assert(seeded.pattern_message_validation && seeded.pattern_message_validation.passed === true, `${persona.id} safety validation failed:${JSON.stringify(seeded)}`);

      const reaction = decideReaction(persona, seeded, { day: daySpec.day });
      await performReaction(page, seeded, reaction);
      const recorded = fixture("persona-record-reaction", {
        flags: {
          "persona-id": persona.id,
          action: reaction.action,
          success: reaction.success,
          "simulation-day": daySpec.day,
        },
      });
      reactions.push(reaction);
      dayResults.push({ day: daySpec.day, pattern: daySpec.pattern, seeded, reaction, recorded });
    }

    const tones = dayResults.map((row) => row.seeded.tone_key);
    const actions = reactions.map((row) => row.action);
    assert(JSON.stringify(tones) === JSON.stringify(scenario.expectedTones), `${persona.id} tone sequence mismatch:${JSON.stringify(tones)}`);
    assert(JSON.stringify(actions) === JSON.stringify(scenario.expectedActions), `${persona.id} action sequence mismatch:${JSON.stringify(actions)}`);

    let suppressedFollowup = null;
    if (actions.includes("suppress")) {
      suppressedFollowup = fixture("persona-suppressed-followup");
    }

    const screenshot = `${persona.id}-final.png`;
    await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
    screenshots.push(screenshot);

    const summary = {
      personaId: persona.id,
      title: scenario.title,
      tones,
      actions,
      firstTakeDay: dayResults.find((row) => row.reaction.action === "take")?.day || null,
      byTone: summarizeReactions(reactions),
      dayResults,
      suppressedFollowup,
    };
    summary.recommendation = analyzePersonaOutcome(persona, reactions);
    scenario.assertScenario(summary);
    assertRecommendation(summary, scenario.expectedRecommendation);
    results.push({
      id: persona.id,
      title: scenario.title,
      status: "passed",
      elapsedMs: Date.now() - started,
      summary,
      screenshots,
    });
    console.log(`PASS ${persona.id} ${Date.now() - started}ms`);
  } catch (error) {
    const screenshot = `${scenario.personaId}-failed.png`;
    try {
      await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
      screenshots.push(screenshot);
    } catch (_) {
      // best effort only
    }
    results.push({
      id: scenario.personaId,
      title: scenario.title,
      status: "failed",
      elapsedMs: Date.now() - started,
      error: error.message || String(error),
      screenshots,
    });
    console.error(`FAIL ${scenario.personaId} ${Date.now() - started}ms ${error.message || error}`);
  }
}

function writeReport(summary) {
  const lines = [];
  lines.push("# Persona Policy Lab");
  lines.push("");
  lines.push(`- Run ID: ${RUN_ID}`);
  lines.push(`- Base URL: ${BASE_URL}`);
  lines.push(`- Passed: ${summary.passed}`);
  lines.push(`- Failed: ${summary.failed}`);
  lines.push(`- Recommendations: ${path.join(OUT_DIR, "recommendations.json")}`);
  lines.push(`- Recommendation CSV: ${path.join(OUT_DIR, "persona_policy_recommendations.csv")}`);
  lines.push("");
  lines.push("## Recommendation Summary");
  lines.push("");
  lines.push("| Persona | Recommended tone | Strategy | Avoid tones | First take | Reply count | Opt-out count |");
  lines.push("| --- | --- | --- | --- | --- | --- | --- |");
  for (const recommendation of summary.recommendations || []) {
    lines.push(
      `| ${recommendation.persona_id} | ${recommendation.recommended_next_tone} | ${recommendation.strategy} | ${(recommendation.avoid_tones || []).join(", ") || "-"} | ${recommendation.first_take_day || "-"} | ${recommendation.reply_count} | ${recommendation.opt_out_count} |`,
    );
  }
  lines.push("");
  for (const result of summary.results) {
    lines.push(`## ${result.id}`);
    lines.push("");
    lines.push(`- Title: ${result.title}`);
    lines.push(`- Status: ${result.status}`);
    lines.push(`- Elapsed: ${result.elapsedMs}ms`);
    if (result.status === "failed") {
      lines.push(`- Error: ${result.error}`);
    } else {
      lines.push(`- Tones: ${result.summary.tones.join(" -> ")}`);
      lines.push(`- Actions: ${result.summary.actions.join(" -> ")}`);
      lines.push(`- First take day: ${result.summary.firstTakeDay || "-"}`);
      lines.push(`- By tone: ${JSON.stringify(result.summary.byTone)}`);
      lines.push(`- Recommended next tone: ${result.summary.recommendation.recommended_next_tone}`);
      lines.push(`- Strategy: ${result.summary.recommendation.strategy}`);
      lines.push(`- Avoid tones: ${result.summary.recommendation.avoid_tones.join(", ") || "-"}`);
      lines.push(`- Rationale: ${result.summary.recommendation.rationale}`);
    }
    if (result.screenshots && result.screenshots.length) {
      lines.push(`- Screenshots: ${result.screenshots.map((name) => path.join(OUT_DIR, name)).join(", ")}`);
    }
    lines.push("");
  }
  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
}

function csvEscape(value) {
  const text = String(value ?? "");
  if (/[",\n]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

function writeRecommendations(summary) {
  const recommendations = summary.recommendations || [];
  fs.writeFileSync(path.join(OUT_DIR, "recommendations.json"), JSON.stringify(recommendations, null, 2), "utf8");
  const headers = [
    "persona_id",
    "recommended_next_tone",
    "strategy",
    "avoid_tones",
    "first_take_day",
    "first_reply_day",
    "take_count",
    "reply_count",
    "opt_out_count",
    "rationale",
  ];
  const rows = recommendations.map((recommendation) => [
    recommendation.persona_id,
    recommendation.recommended_next_tone,
    recommendation.strategy,
    (recommendation.avoid_tones || []).join("|"),
    recommendation.first_take_day || "",
    recommendation.first_reply_day || "",
    recommendation.take_count,
    recommendation.reply_count,
    recommendation.opt_out_count,
    recommendation.rationale,
  ]);
  const csv = [headers, ...rows].map((row) => row.map(csvEscape).join(",")).join("\n");
  fs.writeFileSync(path.join(OUT_DIR, "persona_policy_recommendations.csv"), `${csv}\n`, "utf8");
}

async function main() {
  await getJson("/health");
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  try {
    for (const scenario of personaScenarios) {
      await runPersonaScenario(page, scenario);
    }
  } finally {
    await browser.close();
  }

  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    outputDir: OUT_DIR,
    results,
    recommendations: results
      .filter((result) => result.status === "passed")
      .map((result) => result.summary.recommendation),
    passed: results.filter((result) => result.status === "passed").length,
    failed: results.filter((result) => result.status === "failed").length,
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  writeRecommendations(summary);
  writeReport(summary);
  console.log(JSON.stringify(summary, null, 2));
  if (summary.failed > 0) {
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

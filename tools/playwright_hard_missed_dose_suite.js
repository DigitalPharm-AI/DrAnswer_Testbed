const { chromium } = require("playwright-core");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { analyzePersonaOutcome, decideReaction, summarizeReactions } = require("./playwright_persona_reaction_engine");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8100";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PYTHON = process.env.PYTHON_BIN || path.join(".venv", "Scripts", "python.exe");
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `hard-missed-dose-suite-${RUN_ID}`);
const PROFILES = JSON.parse(fs.readFileSync(path.join("tools", "persona_policy_profiles.json"), "utf8"));

fs.mkdirSync(OUT_DIR, { recursive: true });
const results = [];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function plainText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&quot;/g, '"')
    .replace(/&#34;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
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
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  if (result.status !== 0) {
    throw new Error(`fixture_${command}_failed:${result.stderr || result.stdout}`);
  }
  const stdout = (result.stdout || "").trim();
  return stdout ? JSON.parse(stdout) : {};
}

async function get(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.text();
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

async function waitFor(name, predicate, timeoutMs = 30000, intervalMs = 500) {
  const started = Date.now();
  let lastError = "";
  while (Date.now() - started < timeoutMs) {
    try {
      const value = await predicate();
      if (value) {
        return value;
      }
    } catch (error) {
      lastError = error.message || String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`timeout:${name}${lastError ? ` last=${lastError}` : ""}`);
}

async function gotoPage(page, marker) {
  await page.goto(`${BASE_URL}/?hard=${encodeURIComponent(marker)}&run=${RUN_ID}#chat`, {
    waitUntil: "domcontentloaded",
    timeout: 20000,
  });
  await page.waitForSelector("#chat-panel", { timeout: 10000 });
}

async function capture(page, name) {
  await page.screenshot({ path: path.join(OUT_DIR, name), fullPage: true });
  return name;
}

function activeFlags(inspect) {
  return (inspect.missed_dose_flags || []).filter((flag) => flag.active);
}

function inactiveFlags(inspect) {
  return (inspect.missed_dose_flags || []).filter((flag) => !flag.active);
}

async function chatText() {
  return plainText(await get("/partials/chat-log"));
}

async function notificationText() {
  return plainText(await get("/partials/notifications"));
}

function topTimeFromIso(isoValue) {
  const [date, timeWithSeconds] = String(isoValue || "").split("T");
  return `${date} ${(timeWithSeconds || "").slice(0, 5)}`.trim();
}

async function assertNoClinicianEscalationInFeed() {
  const payload = await getJson("/api/notifications/feed?after_id=0");
  const leaked = (payload.notifications || []).filter((row) => row.notification_type === "clinician_escalation");
  assert(leaked.length === 0, `clinician escalation leaked to patient feed:${JSON.stringify(leaked)}`);
}

async function performPersonaReaction(seeded, reaction) {
  if (reaction.action === "take" && seeded.related_dose_event_id) {
    await postForm(`/doses/${seeded.related_dose_event_id}/take`);
  }
  if (reaction.action === "suppress") {
    await postForm("/reminders/suppression", { suppressed: "true" });
  }
}

function personaPatternDays(personaId) {
  if (personaId === "routine_empathy_responder") return ["A", "A", "A", "A", "A"];
  if (personaId === "side_effect_reporter") return ["C", "C", "C", "C", "C"];
  if (personaId === "low_adherence_warning_responder") return ["E", "E", "E", "E", "E"];
  if (personaId === "anxious_support_responder" || personaId === "warning_avoidant_conversationalist") return ["B", "B", "B", "B", "D"];
  return ["B", "B", "B", "B", "B"];
}

function assertPersonaOutcome(personaId, summary) {
  if (personaId === "lazy_warning_responder") {
    assert(summary.firstTakeDay === 5, `lazy persona should convert on warning_soft day 5:${JSON.stringify(summary)}`);
  }
  if (personaId === "busy_practical_responder") {
    assert(summary.firstTakeDay === 3, `busy persona should convert on practical day 3:${JSON.stringify(summary)}`);
  }
  if (personaId === "anxious_support_responder") {
    assert(summary.actions.includes("suppress"), `anxious persona should suppress on warning_soft:${JSON.stringify(summary)}`);
  }
  if (personaId === "side_effect_reporter") {
    assert(summary.tones.every((tone) => tone === "side_effect_check"), `side effect persona should stay side_effect_check:${JSON.stringify(summary)}`);
    assert(summary.actions.every((action) => action === "reply"), `side effect persona should reply:${JSON.stringify(summary)}`);
  }
  if (personaId === "unresponsive_explore_target") {
    assert(summary.recommendation.strategy === "explore_practical", `unresponsive persona should become exploration target:${JSON.stringify(summary)}`);
  }
  if (personaId === "low_adherence_warning_responder") {
    assert(summary.firstTakeDay === 1, `low adherence persona should take on warning day 1:${JSON.stringify(summary)}`);
  }
}

const scenarios = [
  {
    id: "ROUTINE_FLAG_AND_SLOT_SPLIT",
    title: "아침 missed flag는 점심 복용 후 해제되고 다음날 아침 문제는 slot 단위로 분리된다",
    run: async (page, screenshots) => {
      const first = fixture("seed-current-flag-flow");
      assert(first.pattern_code === "B", `first morning should be B:${JSON.stringify(first)}`);
      assert(activeFlags(first).length === 1, `morning flag should be active:${JSON.stringify(first.missed_dose_flags)}`);
      await gotoPage(page, "ROUTINE_FLAG_ACTIVE");
      assert((await page.locator("#chat-panel").innerText()).includes(first.chat_content), "first missed-dose chat not visible");
      screenshots.push(await capture(page, "ROUTINE_FLAG_ACTIVE.png"));

      await postForm(`/doses/${first.current_flow.lunch_event_id}/take`);
      const afterLunch = fixture("inspect");
      assert(activeFlags(afterLunch).length === 0, `flag should clear after lunch taken:${JSON.stringify(afterLunch.missed_dose_flags)}`);
      assert(
        inactiveFlags(afterLunch).some((flag) => flag.clear_reason === "subsequent_same_day_taken"),
        `flag clear reason should be subsequent_same_day_taken:${JSON.stringify(afterLunch.missed_dose_flags)}`,
      );

      const repeat = fixture("seed-current-repeat-morning-flow");
      assert(repeat.pattern_code === "B", `repeat morning should stay routine scoped B:${JSON.stringify(repeat)}`);
      assert(repeat.streak_metrics.current_consecutive_missed_days === 2, `morning streak should be 2:${JSON.stringify(repeat.streak_metrics)}`);
      assert(repeat.tone_policy.slot_label === "아침 08:00", `tone policy should be scoped to morning:${JSON.stringify(repeat.tone_policy)}`);
      assert(activeFlags(repeat)[0].related_dose_event_id === repeat.current_flow.day2_morning_event_id, `active flag should point to day2 morning:${JSON.stringify(repeat)}`);
      await gotoPage(page, "ROUTINE_REPEAT_MORNING");
      assert((await page.locator("#chat-panel").innerText()).includes(repeat.chat_content), "repeat missed-dose chat not visible");
      screenshots.push(await capture(page, "ROUTINE_REPEAT_MORNING.png"));
      return { first, afterLunch, repeat };
    },
  },
  {
    id: "LLM_SAFETY_FALLBACK",
    title: "LLM 생성 후보가 약물명/강한 표현을 포함하면 CSV fallback만 채팅에 노출된다",
    run: async (page, screenshots) => {
      const seeded = fixture("seed-hard-llm-safety-fallback");
      const llm = seeded.latest_llm_personalization || {};
      assert(seeded.pattern_code === "B", `expected B fallback scenario:${JSON.stringify(seeded)}`);
      assert(llm.message_source === "csv_fallback", `unsafe LLM candidate should fallback:${JSON.stringify(llm)}`);
      assert(llm.fallback_reason === "llm_message_failed_safety_validation", `fallback reason mismatch:${JSON.stringify(llm)}`);
      assert((llm.llm_message_validation?.errors || []).some((error) => error.startsWith("forbidden_term:")), `forbidden term error missing:${JSON.stringify(llm)}`);
      assert(seeded.chat_content === seeded.tone_policy.message, `chat should use tone fallback:${JSON.stringify(seeded)}`);
      assert(!seeded.chat_content.includes("당뇨약"), `unsafe medication name leaked to chat:${seeded.chat_content}`);
      await gotoPage(page, "LLM_SAFETY_FALLBACK");
      const text = await page.locator("#chat-panel").innerText();
      assert(text.includes(seeded.chat_content), `fallback message not visible:${text}`);
      assert(!text.includes("놓치면 위험해요"), `unsafe LLM text leaked to UI:${text}`);
      screenshots.push(await capture(page, "LLM_SAFETY_FALLBACK.png"));
      return { seeded };
    },
  },
  {
    id: "PRIORITY_AND_ESCALATION_COLLISIONS",
    title: "C>D 우선순위, D/E 내부 escalation, 사용자 feed 숨김을 함께 검증한다",
    run: async (page, screenshots) => {
      const c = fixture("seed-complex", { scenario: "C_SUPPRESSION_AFTER_KEEP" });
      assert(c.pattern_code === "C", `C should win over D collision:${JSON.stringify(c)}`);
      assert(c.streak_metrics.current_consecutive_missed_days >= 3, `C fixture should collide with D:${JSON.stringify(c.streak_metrics)}`);
      assert(c.tone_key === "side_effect_check", `C tone should be side_effect_check:${JSON.stringify(c.tone_policy)}`);
      await gotoPage(page, "PRIORITY_C");
      assert((await page.locator("#chat-panel").innerText()).includes(c.chat_content), "C priority chat not visible");
      screenshots.push(await capture(page, "PRIORITY_C.png"));

      const d = fixture("seed-complex", { scenario: "D_ESCALATION_IDEMPOTENCY" });
      assert(d.pattern_code === "D", `expected D:${JSON.stringify(d)}`);
      assert(d.clinician_escalation_count === 1, `D escalation missing:${JSON.stringify(d)}`);
      await assertNoClinicianEscalationInFeed();

      const e = fixture("seed-complex", { scenario: "E_REPLY_WITH_MIXED_SLOTS" });
      assert(e.pattern_code === "E", `expected E:${JSON.stringify(e)}`);
      assert(e.streak_metrics.current_consecutive_missed_days === 2, `E should not be D:${JSON.stringify(e.streak_metrics)}`);
      assert(e.streak_metrics.overall_adherence_rate < 0.5, `E adherence rate should be low:${JSON.stringify(e.streak_metrics)}`);
      assert(e.clinician_escalation_count === 1, `E escalation missing:${JSON.stringify(e)}`);
      await assertNoClinicianEscalationInFeed();
      await gotoPage(page, "PRIORITY_E");
      assert((await page.locator("#chat-panel").innerText()).includes(e.chat_content), "E priority chat not visible");
      screenshots.push(await capture(page, "PRIORITY_E.png"));
      return { c, d, e };
    },
  },
  {
    id: "PERSONA_LONGITUDINAL_TEN_PROFILES",
    title: "10개 persona가 metadata tone에 반응하고 성공/실패 이력이 다음 tone 선택에 반영된다",
    run: async (page, screenshots) => {
      const summaries = [];
      for (const persona of PROFILES) {
        fixture("persona-reset");
        const reactions = [];
        const rows = [];
        const days = personaPatternDays(persona.id);
        for (let index = 0; index < days.length; index += 1) {
          const day = index + 1;
          const seeded = fixture("persona-seed-round", { flags: { pattern: days[index], "simulation-day": day } });
          await gotoPage(page, `PERSONA_${persona.id}_${day}`);
          assert((await page.locator("#chat-panel").innerText()).includes(seeded.chat_content), `${persona.id} day ${day} chat not visible`);
          const reaction = decideReaction(persona, seeded, { day });
          assert(reaction.action === persona.rules[seeded.tone_key], `${persona.id} reaction did not follow metadata tone:${JSON.stringify({ seeded, reaction })}`);
          await performPersonaReaction(seeded, reaction);
          const recorded = fixture("persona-record-reaction", {
            flags: {
              "persona-id": persona.id,
              action: reaction.action,
              success: reaction.success,
              "simulation-day": day,
            },
          });
          assert(recorded.persona_reaction.action === reaction.action, `${persona.id} reaction metadata not recorded:${JSON.stringify(recorded.persona_reaction)}`);
          reactions.push(reaction);
          rows.push({ day, pattern: days[index], tone: seeded.tone_key, action: reaction.action });
        }
        const summary = {
          personaId: persona.id,
          tones: reactions.map((reaction) => reaction.tone_key),
          actions: reactions.map((reaction) => reaction.action),
          firstTakeDay: reactions.find((reaction) => reaction.action === "take")?.simulation_day || null,
          byTone: summarizeReactions(reactions),
          recommendation: analyzePersonaOutcome(persona, reactions),
          rows,
        };
        assertPersonaOutcome(persona.id, summary);
        summaries.push(summary);
      }
      await gotoPage(page, "PERSONA_FINAL");
      screenshots.push(await capture(page, "PERSONA_LONGITUDINAL_TEN_PROFILES.png"));
      return { summaries };
    },
  },
  {
    id: "SYSTEM_TIME_UI_DB_ALIGNMENT",
    title: "상단 시간, feed current_time, 채팅 timestamp가 simulation clock 기준으로 맞는다",
    run: async (page, screenshots) => {
      const messageSeed = fixture("seed-current-flag-flow");
      assert(messageSeed.message_created_at === messageSeed.clock_current_time, `chat timestamp should use simulation clock:${JSON.stringify(messageSeed)}`);
      const seeded = fixture("seed-current-ready-clock");
      await gotoPage(page, "SYSTEM_TIME_ALIGNMENT");
      const topTime = (await page.locator("#time-bar .top-time-value").innerText()).trim();
      assert(topTime.includes(topTimeFromIso(seeded.clock_current_time)), `top time should show simulation time:${topTime}:${JSON.stringify(seeded)}`);
      const feed = await getJson("/api/notifications/feed?after_id=0");
      assert(feed.current_time.startsWith(seeded.clock_current_time), `feed current_time mismatch:${JSON.stringify(feed)} seeded=${seeded.clock_current_time}`);

      await postForm("/clock/play", { speed_multiplier: "60" });
      const after = await waitFor(
        "top_time_live_update",
        async () => {
          const value = (await page.locator("#time-bar .top-time-value").innerText()).trim();
          return value !== topTime ? value : "";
        },
        15000,
        500,
      );
      await postForm("/clock/pause");
      assert(!after.includes("2026-06-05"), `top time used real date instead of simulation time:${after}`);
      screenshots.push(await capture(page, "SYSTEM_TIME_UI_DB_ALIGNMENT.png"));
      return { messageSeed, seeded, topTime, after };
    },
  },
  {
    id: "SUPPRESSION_GUARD_NO_MISSED_CHAT",
    title: "side-effect suppression 상태에서는 missed event는 기록되어도 미복용 채팅 알림은 생성되지 않는다",
    run: async (page, screenshots) => {
      const seeded = fixture("seed-suppression");
      assert(seeded.missed_payload_count === 0, `suppression should block missed-dose agent payload:${JSON.stringify(seeded)}`);
      assert(seeded.missed_dose_message_count === 0, `suppression should block missed-dose chat:${JSON.stringify(seeded)}`);
      assert(seeded.unack_missed_conversation_alert_count === 0, `suppression should block conversation alert:${JSON.stringify(seeded)}`);
      assert(seeded.missed_dose_flags.length === 1 && seeded.missed_dose_flags[0].active, `missed flag should still record the state:${JSON.stringify(seeded.missed_dose_flags)}`);
      await gotoPage(page, "SUPPRESSION_GUARD");
      const text = await page.locator("#chat-panel").innerText();
      assert(!text.includes("미복용 대화"), `suppressed missed-dose chat leaked:${text}`);
      screenshots.push(await capture(page, "SUPPRESSION_GUARD_NO_MISSED_CHAT.png"));
      return { seeded };
    },
  },
  {
    id: "AGENT_FAILURE_NO_PROCESSING_STUCK",
    title: "agent timeout/failure가 나도 알림 UI가 AI 처리 중에 고착되지 않는다",
    run: async (page, screenshots) => {
      const seeded = fixture("seed-hard-agent-failure-no-stuck");
      assert(seeded.agent_error_count === 1, `agent_error notification missing:${JSON.stringify(seeded)}`);
      assert(seeded.agent_jobs.some((job) => job.status === "failed"), `failed agent job missing:${JSON.stringify(seeded.agent_jobs)}`);
      await page.goto(`${BASE_URL}/?hard=AGENT_FAILURE_NO_STUCK&run=${RUN_ID}`, {
        waitUntil: "domcontentloaded",
        timeout: 20000,
      });
      const text = await notificationText();
      assert(text.includes("AI 처리 실패"), `agent failure text not visible:${text}`);
      assert(text.includes("다시 시도"), `retry action not visible:${text}`);
      assert(!text.includes("AI 처리 중"), `notification UI is still stuck in processing:${text}`);
      screenshots.push(await capture(page, "AGENT_FAILURE_NO_PROCESSING_STUCK.png"));
      return { seeded };
    },
  },
];

async function runScenario(page, scenario) {
  const started = Date.now();
  const screenshots = [];
  try {
    const details = await scenario.run(page, screenshots);
    results.push({
      id: scenario.id,
      title: scenario.title,
      status: "passed",
      elapsedMs: Date.now() - started,
      details,
      screenshots,
    });
    console.log(`PASS ${scenario.id} ${Date.now() - started}ms`);
  } catch (error) {
    const screenshot = `${scenario.id}-failed.png`;
    try {
      screenshots.push(await capture(page, screenshot));
    } catch (_) {
      // Best effort only.
    }
    results.push({
      id: scenario.id,
      title: scenario.title,
      status: "failed",
      elapsedMs: Date.now() - started,
      error: error.message || String(error),
      screenshots,
    });
    console.error(`FAIL ${scenario.id} ${Date.now() - started}ms ${error.message || error}`);
  }
}

function writeReport(summary) {
  const lines = [];
  lines.push("# Hard Missed Dose Playwright Suite");
  lines.push("");
  lines.push(`- Run ID: ${RUN_ID}`);
  lines.push(`- Base URL: ${BASE_URL}`);
  lines.push(`- Passed: ${summary.passed}`);
  lines.push(`- Failed: ${summary.failed}`);
  lines.push("");
  lines.push("| Scenario | Status | Elapsed |");
  lines.push("| --- | --- | --- |");
  for (const result of summary.results) {
    lines.push(`| ${result.id} | ${result.status} | ${result.elapsedMs}ms |`);
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
    }
    if (result.screenshots && result.screenshots.length) {
      lines.push(`- Screenshots: ${result.screenshots.map((name) => path.join(OUT_DIR, name)).join(", ")}`);
    }
    if (result.details?.summaries) {
      lines.push("- Persona recommendations:");
      for (const item of result.details.summaries) {
        lines.push(
          `  - ${item.personaId}: ${item.recommendation.recommended_next_tone} / ${item.recommendation.strategy} / actions=${item.actions.join(" -> ")}`,
        );
      }
    }
    lines.push("");
  }
  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
}

async function main() {
  await getJson("/health");
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  try {
    for (const scenario of scenarios) {
      await runScenario(page, scenario);
    }
  } finally {
    await browser.close();
  }

  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    outputDir: OUT_DIR,
    results,
    passed: results.filter((result) => result.status === "passed").length,
    failed: results.filter((result) => result.status === "failed").length,
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
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

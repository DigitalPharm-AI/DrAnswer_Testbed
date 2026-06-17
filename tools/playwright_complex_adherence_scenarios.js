const { chromium } = require("playwright-core");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8100";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PYTHON = process.env.PYTHON_BIN || path.join(".venv", "Scripts", "python.exe");
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `complex-adherence-scenarios-${RUN_ID}`);

const PATTERN_MESSAGES = {
  C: "불편한 증상이 있나요? 상태를 먼저 알려주세요.",
  D: "며칠째 기록이 비어 있어요. 의료진과 함께 확인할게요.",
  E: "최근 기록이 많이 비어 있어요. 의료진과 함께 확인할게요.",
};

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

async function waitFor(name, predicate, timeoutMs = 60000, intervalMs = 500) {
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

function fixture(command, scenario = "") {
  const args = ["tools/playwright_adherence_pattern_fixture.py", command];
  if (scenario) {
    args.push(scenario);
  }
  const result = spawnSync(PYTHON, args, {
    cwd: process.cwd(),
    encoding: "utf8",
    env: process.env,
  });
  if (result.status !== 0) {
    throw new Error(`fixture_${command}_failed:${result.stderr || result.stdout}`);
  }
  return JSON.parse((result.stdout || "{}").trim() || "{}");
}

async function chatText() {
  return plainText(await get("/partials/chat-log"));
}

async function assertNoClinicianEscalationInPatientFeed() {
  const payload = await getJson("/api/notifications/feed?after_id=0");
  const leaked = (payload.notifications || []).filter((row) => row.notification_type === "clinician_escalation");
  assert(leaked.length === 0, `clinician escalation leaked to feed:${JSON.stringify(leaked)}`);
}

async function gotoChat(page, scenarioId) {
  await page.goto(`${BASE_URL}/?complex=${encodeURIComponent(scenarioId)}&run=${RUN_ID}#chat`, {
    waitUntil: "domcontentloaded",
    timeout: 20000,
  });
  await page.waitForSelector("#chat-panel", { timeout: 10000 });
}

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
      await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
      screenshots.push(screenshot);
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

const scenarios = [
  {
    id: "C_SUPPRESSION_AFTER_KEEP",
    title: "부작용 유지 우선순위 후 suppression guard",
    run: async (page, screenshots) => {
      const initial = fixture("seed-complex", "C_SUPPRESSION_AFTER_KEEP");
      assert(initial.pattern_code === "C", `expected C priority:${JSON.stringify(initial)}`);
      assert(initial.clinician_escalation_count === 0, `C should not escalate despite long streak:${JSON.stringify(initial)}`);
      assert(initial.streak_metrics.current_consecutive_missed_days >= 3, `C fixture must collide with D:${JSON.stringify(initial)}`);
      assert(initial.streak_metrics.recent_side_effect_keep === true, `C side-effect keep missing:${JSON.stringify(initial)}`);
      await gotoChat(page, "C_SUPPRESSION_AFTER_KEEP");
      assert((await page.locator("#chat-panel").innerText()).includes(PATTERN_MESSAGES.C), "C pattern message not visible");
      const firstScreenshot = "C_SUPPRESSION_AFTER_KEEP-initial.png";
      await page.screenshot({ path: path.join(OUT_DIR, firstScreenshot), fullPage: true });
      screenshots.push(firstScreenshot);

      const afterSuppression = fixture("suppress-next-missed");
      assert(afterSuppression.next_event_status === "missed", `suppressed follow-up event was not marked missed:${JSON.stringify(afterSuppression)}`);
      assert(afterSuppression.missed_payload_count === 0, `suppressed follow-up emitted agent payload:${JSON.stringify(afterSuppression)}`);
      assert(
        afterSuppression.conversation_alert_count === afterSuppression.conversation_alert_count_before_followup,
        `suppression created a new conversation alert:${JSON.stringify(afterSuppression)}`,
      );
      assert(
        afterSuppression.missed_dose_message_count === afterSuppression.missed_dose_message_count_before_followup,
        `suppression created a new missed-dose chat:${JSON.stringify(afterSuppression)}`,
      );
      await gotoChat(page, "C_SUPPRESSION_AFTER_KEEP_AFTER_SUPPRESSION");
      const secondScreenshot = "C_SUPPRESSION_AFTER_KEEP-suppressed.png";
      await page.screenshot({ path: path.join(OUT_DIR, secondScreenshot), fullPage: true });
      screenshots.push(secondScreenshot);
      return { initial, afterSuppression };
    },
  },
  {
    id: "D_ESCALATION_IDEMPOTENCY",
    title: "장기 미복용 의료진 스텁 숨김과 중복 방지",
    run: async (page, screenshots) => {
      const initial = fixture("seed-complex", "D_ESCALATION_IDEMPOTENCY");
      assert(initial.pattern_code === "D", `expected D:${JSON.stringify(initial)}`);
      assert(initial.clinician_escalation_count === 1, `D escalation missing:${JSON.stringify(initial)}`);
      assert(initial.unack_missed_conversation_alert_count === 1, `D missed alert should be active:${JSON.stringify(initial)}`);
      const metadata = initial.clinician_escalation_metadata[0] || {};
      assert(metadata.delivery_channel === "internal_only", `D escalation is not internal-only:${JSON.stringify(metadata)}`);
      await assertNoClinicianEscalationInPatientFeed();
      await gotoChat(page, "D_ESCALATION_IDEMPOTENCY");
      assert((await page.locator("#chat-panel").innerText()).includes(PATTERN_MESSAGES.D), "D pattern message not visible");
      const initialScreenshot = "D_ESCALATION_IDEMPOTENCY-initial.png";
      await page.screenshot({ path: path.join(OUT_DIR, initialScreenshot), fullPage: true });
      screenshots.push(initialScreenshot);

      const replayed = fixture("replay-latest-missed");
      assert(
        replayed.clinician_escalation_count === replayed.clinician_escalation_count_before_replay,
        `D replay duplicated clinician escalation:${JSON.stringify(replayed)}`,
      );
      assert(
        replayed.missed_dose_message_count === replayed.missed_dose_message_count_before_replay,
        `D replay duplicated missed-dose chat:${JSON.stringify(replayed)}`,
      );
      await assertNoClinicianEscalationInPatientFeed();
      return { initial, replayed };
    },
  },
  {
    id: "E_REPLY_WITH_MIXED_SLOTS",
    title: "전반적 저조 E와 다른 시간대 복용 성공 혼합 후 멀티턴 답변",
    run: async (page, screenshots) => {
      const initial = fixture("seed-complex", "E_REPLY_WITH_MIXED_SLOTS");
      assert(initial.pattern_code === "E", `expected E:${JSON.stringify(initial)}`);
      assert(initial.clinician_escalation_count === 1, `E escalation missing:${JSON.stringify(initial)}`);
      assert(initial.streak_metrics.current_consecutive_missed_days === 2, `E should be two current misses, not D:${JSON.stringify(initial)}`);
      assert(initial.streak_metrics.overall_adherence_rate < 0.5, `E adherence rate not low:${JSON.stringify(initial)}`);
      assert(initial.streak_metrics.overall_taken_count >= 6, `E fixture should include successful other-slot doses:${JSON.stringify(initial)}`);
      assert(!initial.chat_content.includes("당뇨약"), `pattern message leaked medication name:${initial.chat_content}`);
      await assertNoClinicianEscalationInPatientFeed();
      await gotoChat(page, "E_REPLY_WITH_MIXED_SLOTS");
      assert((await page.locator("#chat-panel").innerText()).includes(PATTERN_MESSAGES.E), "E pattern message not visible");
      const initialScreenshot = "E_REPLY_WITH_MIXED_SLOTS-initial.png";
      await page.screenshot({ path: path.join(OUT_DIR, initialScreenshot), fullPage: true });
      screenshots.push(initialScreenshot);

      const unique = `complex-e-reply-${RUN_ID}`;
      await postForm("/chat/system", { event_type: "multiturn_chat", message: unique });
      await waitFor(
        "complex_e_multiturn_reply",
        async () => {
          const text = await chatText();
          return text.includes(unique) && (text.includes("앞선 대화 맥락") || text.includes("말씀을 확인했습니다"));
        },
        60000,
        1000,
      );
      const afterReply = fixture("inspect");
      assert(afterReply.unack_missed_conversation_alert_count === 0, `missed alert was not acknowledged by /chat/system reply:${JSON.stringify(afterReply)}`);
      await gotoChat(page, "E_REPLY_WITH_MIXED_SLOTS_AFTER_REPLY");
      const replyScreenshot = "E_REPLY_WITH_MIXED_SLOTS-reply.png";
      await page.screenshot({ path: path.join(OUT_DIR, replyScreenshot), fullPage: true });
      screenshots.push(replyScreenshot);
      return { initial, afterReply, unique };
    },
  },
];

function writeReport(summary) {
  const lines = [];
  lines.push("# Complex Adherence Playwright Scenarios");
  lines.push("");
  lines.push(`- Run ID: ${RUN_ID}`);
  lines.push(`- Base URL: ${BASE_URL}`);
  lines.push(`- Passed: ${summary.passed}`);
  lines.push(`- Failed: ${summary.failed}`);
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

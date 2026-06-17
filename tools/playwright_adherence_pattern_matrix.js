const { chromium } = require("playwright-core");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8100";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PYTHON = process.env.PYTHON_BIN || path.join(".venv", "Scripts", "python.exe");
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `adherence-pattern-matrix-${RUN_ID}`);

const PATTERN_MESSAGES = {
  A: "잠시 놓치신 것 같아요. 지금 상태를 알려주세요.",
  B: "복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
  C: "불편한 증상이 있나요? 상태를 먼저 알려주세요.",
  D: "며칠째 기록이 비어 있어요. 의료진과 함께 확인할게요.",
  E: "최근 기록이 많이 비어 있어요. 의료진과 함께 확인할게요.",
};

const scenarios = [
  {
    id: "A",
    title: "A 단순 망각",
    expectedCode: "A",
    expectedMessage: PATTERN_MESSAGES.A,
    expectedEscalation: false,
    assertMetrics: (metrics) => {
      assert(metrics.current_consecutive_missed_days === 1, `A current streak mismatch:${JSON.stringify(metrics)}`);
      assert(metrics.previous_consecutive_taken_days >= 14, `A previous taken streak mismatch:${JSON.stringify(metrics)}`);
    },
  },
  {
    id: "B",
    title: "B 습관 미형성",
    expectedCode: "B",
    expectedMessage: PATTERN_MESSAGES.B,
    expectedEscalation: false,
    assertMetrics: (metrics) => {
      assert(metrics.current_consecutive_missed_days === 1, `B current streak mismatch:${JSON.stringify(metrics)}`);
      assert(metrics.previous_consecutive_taken_days < 7, `B previous taken streak mismatch:${JSON.stringify(metrics)}`);
    },
  },
  {
    id: "C",
    title: "C 부작용 가능성 우선순위",
    expectedCode: "C",
    expectedMessage: PATTERN_MESSAGES.C,
    expectedEscalation: false,
    assertMetrics: (metrics) => {
      assert(metrics.recent_side_effect_keep === true, `C side effect keep mismatch:${JSON.stringify(metrics)}`);
      assert(metrics.current_consecutive_missed_days >= 3, `C priority fixture did not include D collision:${JSON.stringify(metrics)}`);
    },
  },
  {
    id: "D",
    title: "D 장기 연속 미복용",
    expectedCode: "D",
    expectedMessage: PATTERN_MESSAGES.D,
    expectedEscalation: true,
    assertMetrics: (metrics) => {
      assert(metrics.current_consecutive_missed_days >= 3, `D current streak mismatch:${JSON.stringify(metrics)}`);
    },
  },
  {
    id: "E",
    title: "E 전반적 저조",
    expectedCode: "E",
    expectedMessage: PATTERN_MESSAGES.E,
    expectedEscalation: true,
    assertMetrics: (metrics) => {
      assert(metrics.current_consecutive_missed_days === 2, `E current streak mismatch:${JSON.stringify(metrics)}`);
      assert(metrics.overall_adherence_rate < 0.5, `E adherence rate mismatch:${JSON.stringify(metrics)}`);
      assert(metrics.prescription_day_count >= 15, `E prescription days mismatch:${JSON.stringify(metrics)}`);
    },
  },
  {
    id: "SLOT_SCOPED",
    title: "시간대별 streak 계산",
    expectedCode: "B",
    expectedMessage: PATTERN_MESSAGES.B,
    expectedEscalation: false,
    assertMetrics: (metrics) => {
      assert(metrics.current_consecutive_missed_days === 2, `slot streak diluted by other slot:${JSON.stringify(metrics)}`);
      assert(metrics.overall_taken_count >= 3, `slot fixture did not include other-slot taken events:${JSON.stringify(metrics)}`);
    },
  },
];

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
  const stdout = (result.stdout || "").trim();
  return stdout ? JSON.parse(stdout) : {};
}

async function chatText() {
  return plainText(await get("/partials/chat-log"));
}

async function assertNoHiddenEscalationInFeed() {
  const payload = await getJson("/api/notifications/feed?after_id=0");
  const visibleEscalations = (payload.notifications || []).filter((row) => row.notification_type === "clinician_escalation");
  assert(visibleEscalations.length === 0, `clinician escalation leaked to patient feed:${JSON.stringify(visibleEscalations)}`);
}

async function runScenario(page, scenario) {
  const started = Date.now();
  const screenshots = [];
  try {
    await postForm("/simulation/reset");
    const seeded = fixture("seed", scenario.id);
    await page.goto(`${BASE_URL}/?scenario=${encodeURIComponent(scenario.id)}#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForSelector("#chat-panel", { timeout: 10000 });
    const text = await page.locator("#chat-panel").innerText();
    assert(text.includes(scenario.expectedMessage), `${scenario.id} message not visible in chat:${text}`);

    const inspect = fixture("inspect");
    assert(inspect.pattern_code === scenario.expectedCode, `${scenario.id} pattern mismatch:${JSON.stringify(inspect)}`);
    assert(inspect.chat_content === scenario.expectedMessage, `${scenario.id} chat content mismatch:${JSON.stringify(inspect)}`);
    assert(inspect.pattern_message_validation && inspect.pattern_message_validation.passed === true, `${scenario.id} safety validation failed:${JSON.stringify(inspect)}`);
    scenario.assertMetrics(inspect.streak_metrics || {});

    if (scenario.expectedEscalation) {
      assert(inspect.clinician_escalation_count === 1, `${scenario.id} escalation stub missing:${JSON.stringify(inspect)}`);
      const metadata = inspect.clinician_escalation_metadata[0] || {};
      assert(metadata.category === "clinician_escalation", `${scenario.id} escalation category mismatch:${JSON.stringify(metadata)}`);
      assert(metadata.delivery_channel === "internal_only", `${scenario.id} escalation delivery mismatch:${JSON.stringify(metadata)}`);
      assert(metadata.pattern_code === scenario.expectedCode, `${scenario.id} escalation code mismatch:${JSON.stringify(metadata)}`);
      await assertNoHiddenEscalationInFeed();
    } else {
      assert(inspect.clinician_escalation_count === 0, `${scenario.id} unexpected escalation stub:${JSON.stringify(inspect)}`);
    }

    const screenshot = `${scenario.id}-chat.png`;
    await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
    screenshots.push(screenshot);
    results.push({
      id: scenario.id,
      title: scenario.title,
      status: "passed",
      elapsedMs: Date.now() - started,
      seeded,
      inspect,
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

async function runSuppressionScenario(page) {
  const started = Date.now();
  const id = "SUPPRESSION";
  try {
    await postForm("/simulation/reset");
    const inspect = fixture("seed-suppression");
    await page.goto(`${BASE_URL}/?scenario=SUPPRESSION#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    const text = await chatText();
    assert(inspect.missed_payload_count === 0, `suppression payloads were generated:${JSON.stringify(inspect)}`);
    assert(inspect.conversation_alert_count === 0, `suppression created conversation alerts:${JSON.stringify(inspect)}`);
    assert(inspect.missed_dose_message_count === 0, `suppression created missed-dose chat:${JSON.stringify(inspect)}`);
    assert(!text.includes("미복용 대화"), `suppression leaked missed-dose chat:${text}`);
    await page.screenshot({ path: path.join(OUT_DIR, "SUPPRESSION-chat.png"), fullPage: true });
    results.push({ id, title: "부작용 suppression 상태", status: "passed", elapsedMs: Date.now() - started, inspect });
    console.log(`PASS ${id} ${Date.now() - started}ms`);
  } catch (error) {
    results.push({ id, title: "부작용 suppression 상태", status: "failed", elapsedMs: Date.now() - started, error: error.message || String(error) });
    console.error(`FAIL ${id} ${Date.now() - started}ms ${error.message || error}`);
  }
}

async function runSystemChatRouteScenario(page) {
  const started = Date.now();
  const id = "SYSTEM_CHAT_ROUTE";
  const unique = `playwright-system-chat-${RUN_ID}`;
  try {
    await postForm("/simulation/reset");
    await page.goto(`${BASE_URL}/?scenario=SYSTEM_CHAT_ROUTE#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await postForm("/chat/system", { event_type: "multiturn_chat", message: unique });
    await waitFor(
      "system_chat_multiturn_response",
      async () => {
        const text = await chatText();
        return text.includes(unique) && (text.includes("말씀을 확인했습니다") || text.includes("앞선 대화 맥락"));
      },
      60000,
      1000,
    );
    await page.goto(`${BASE_URL}/?scenario=SYSTEM_CHAT_ROUTE_DONE#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.screenshot({ path: path.join(OUT_DIR, "SYSTEM_CHAT_ROUTE-chat.png"), fullPage: true });
    results.push({ id, title: "/chat/system 멀티턴 경로", status: "passed", elapsedMs: Date.now() - started, unique });
    console.log(`PASS ${id} ${Date.now() - started}ms`);
  } catch (error) {
    results.push({ id, title: "/chat/system 멀티턴 경로", status: "failed", elapsedMs: Date.now() - started, error: error.message || String(error) });
    console.error(`FAIL ${id} ${Date.now() - started}ms ${error.message || error}`);
  }
}

function writeReport(summary) {
  const lines = [];
  lines.push("# Playwright Adherence Pattern Matrix");
  lines.push("");
  lines.push(`- Run ID: ${RUN_ID}`);
  lines.push(`- Base URL: ${BASE_URL}`);
  lines.push(`- Passed: ${summary.passed}`);
  lines.push(`- Failed: ${summary.failed}`);
  lines.push("");
  for (const result of summary.results) {
    lines.push(`## ${result.id} - ${result.title}`);
    lines.push("");
    lines.push(`- Status: ${result.status}`);
    lines.push(`- Elapsed: ${result.elapsedMs}ms`);
    if (result.status === "failed") {
      lines.push(`- Error: ${result.error}`);
    } else if (result.inspect) {
      lines.push(`- Pattern: ${result.inspect.pattern_code || "-"}`);
      lines.push(`- Chat: ${result.inspect.chat_content || "-"}`);
      lines.push(`- Escalations: ${result.inspect.clinician_escalation_count}`);
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
    await runSuppressionScenario(page);
    await runSystemChatRouteScenario(page);
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

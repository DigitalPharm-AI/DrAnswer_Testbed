const { chromium } = require("playwright-core");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8100";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PYTHON = process.env.PYTHON_BIN || path.join(".venv", "Scripts", "python.exe");
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `current-scenario-advanced-${RUN_ID}`);

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

function fixture(command) {
  const result = spawnSync(PYTHON, ["tools/playwright_adherence_pattern_fixture.py", command], {
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

function activeFlags(inspect) {
  return (inspect.missed_dose_flags || []).filter((flag) => flag.active);
}

function inactiveFlags(inspect) {
  return (inspect.missed_dose_flags || []).filter((flag) => !flag.active);
}

function isCsvCatalogSource(value) {
  return String(value || "").replace(/\\/g, "/").endsWith("/data/missed_dose_tone_messages.csv");
}

async function gotoChat(page, marker) {
  await page.goto(`${BASE_URL}/?advanced=${encodeURIComponent(marker)}&run=${RUN_ID}#chat`, {
    waitUntil: "domcontentloaded",
    timeout: 20000,
  });
  await page.waitForSelector("#chat-panel", { timeout: 10000 });
}

async function capture(page, name) {
  await page.screenshot({ path: path.join(OUT_DIR, name), fullPage: true });
  return name;
}

async function runCase(page, id, title, fn) {
  const started = Date.now();
  const screenshots = [];
  try {
    const details = await fn(screenshots);
    results.push({
      id,
      title,
      status: "passed",
      elapsedMs: Date.now() - started,
      details,
      screenshots,
    });
    console.log(`PASS ${id} ${Date.now() - started}ms`);
  } catch (error) {
    const screenshot = `${id}-failed.png`;
    try {
      screenshots.push(await capture(page, screenshot));
    } catch (_) {
      // Best effort only.
    }
    results.push({
      id,
      title,
      status: "failed",
      elapsedMs: Date.now() - started,
      error: error.message || String(error),
      screenshots,
    });
    console.error(`FAIL ${id} ${Date.now() - started}ms ${error.message || error}`);
  }
}

async function runTimeBarLiveUpdate(page, screenshots) {
  const seeded = fixture("seed-current-ready-clock");
  await gotoChat(page, "time-bar-live-update");
  const before = (await page.locator("#time-bar .top-time-value").innerText()).trim();
  await postForm("/clock/play", { speed_multiplier: "60" });
  const after = await waitFor(
    "time_bar_polling_after_outer_html_swap",
    async () => {
      const value = (await page.locator("#time-bar .top-time-value").innerText()).trim();
      return value !== before ? value : "";
    },
    15000,
    500,
  );
  const fullText = await page.locator("#time-bar").innerText();
  await postForm("/clock/pause");
  screenshots.push(await capture(page, "TIME_BAR_LIVE_UPDATE.png"));
  return { seeded, before, after, fullText };
}

async function runFlagClearedAndChatDetached(page, screenshots) {
  await postForm("/simulation/reset");
  const seeded = fixture("seed-current-flag-flow");
  assert(seeded.pattern_code === "B", `expected initial B:${JSON.stringify(seeded)}`);
  assert(seeded.tone_key === "persuasion", `expected persuasion tone:${JSON.stringify(seeded.tone_policy)}`);
  assert(isCsvCatalogSource(seeded.tone_policy.message_catalog_source), `tone message should come from csv:${JSON.stringify(seeded.tone_policy)}`);
  assert(seeded.message_created_at === seeded.clock_current_time, `chat timestamp did not use simulation clock:${JSON.stringify(seeded)}`);
  assert(activeFlags(seeded).length === 1, `morning missed flag should be active:${JSON.stringify(seeded.missed_dose_flags)}`);
  assert(seeded.chat_content === seeded.tone_policy.message, `chat message and tone catalog message diverged:${JSON.stringify(seeded)}`);
  assert(!seeded.chat_content.includes("당뇨약"), `safe missed-dose message leaked medication name:${seeded.chat_content}`);

  await gotoChat(page, "flag-active");
  const chatPanel = await page.locator("#chat-panel").innerText();
  assert(chatPanel.includes(seeded.chat_content), `seeded missed-dose message not visible:${chatPanel}`);
  screenshots.push(await capture(page, "FLAG_ACTIVE_CHAT.png"));

  await postForm(`/doses/${seeded.current_flow.lunch_event_id}/take`);
  const afterTaken = fixture("inspect");
  assert(activeFlags(afterTaken).length === 0, `flag should be cleared after lunch taken:${JSON.stringify(afterTaken.missed_dose_flags)}`);
  assert(inactiveFlags(afterTaken).some((flag) => flag.clear_reason === "subsequent_same_day_taken"), `flag clear reason mismatch:${JSON.stringify(afterTaken.missed_dose_flags)}`);

  const unique = `advanced-detached-${RUN_ID}`;
  await postForm("/chat/system", { event_type: "multiturn_chat", message: unique });
  await waitFor(
    "detached_system_chat_response",
    async () => {
      const text = await chatText();
      return text.includes(unique) && (text.includes("말씀을 확인했습니다") || text.includes("앞선 대화 맥락"));
    },
    60000,
    1000,
  );
  const afterReply = fixture("inspect");
  assert(!afterReply.latest_user_metadata.missed_dose_reply, `stale missed-dose flag attached to user reply:${JSON.stringify(afterReply.latest_user_metadata)}`);
  assert(!afterReply.latest_system_request_metadata.missed_dose_reply, `stale missed-dose flag attached to system request:${JSON.stringify(afterReply.latest_system_request_metadata)}`);
  await gotoChat(page, "flag-cleared-detached");
  screenshots.push(await capture(page, "FLAG_CLEARED_CHAT_DETACHED.png"));
  return { seeded, afterTaken, afterReply, unique };
}

async function runRepeatMorningSeparated(page, screenshots) {
  await postForm("/simulation/reset");
  const seeded = fixture("seed-current-repeat-morning-flow");
  assert(seeded.pattern_code === "B", `repeat morning should remain B, not whole-day E/D:${JSON.stringify(seeded)}`);
  assert(seeded.streak_metrics.current_consecutive_missed_days === 2, `morning streak should be 2:${JSON.stringify(seeded.streak_metrics)}`);
  assert(isCsvCatalogSource(seeded.tone_policy.message_catalog_source), `repeat tone should come from csv:${JSON.stringify(seeded.tone_policy)}`);
  assert(seeded.tone_policy.slot_label === "아침 08:00", `repeat problem should be scoped to morning:${JSON.stringify(seeded.tone_policy)}`);
  assert(activeFlags(seeded).length === 1, `day2 missed flag should be active:${JSON.stringify(seeded.missed_dose_flags)}`);
  assert(inactiveFlags(seeded).length >= 1, `day1 flag should remain inactive history:${JSON.stringify(seeded.missed_dose_flags)}`);
  assert(activeFlags(seeded)[0].related_dose_event_id === seeded.current_flow.day2_morning_event_id, `active flag should point to day2 morning:${JSON.stringify(seeded)}`);
  assert(inactiveFlags(seeded).some((flag) => flag.clear_reason === "subsequent_same_day_taken"), `day1 lunch clear reason missing:${JSON.stringify(seeded.missed_dose_flags)}`);
  assert(!seeded.chat_content.includes("당뇨약"), `repeat message leaked medication name:${seeded.chat_content}`);

  await gotoChat(page, "repeat-morning-separated");
  const chatPanel = await page.locator("#chat-panel").innerText();
  assert(chatPanel.includes(seeded.chat_content), `repeat missed-dose message not visible:${chatPanel}`);
  screenshots.push(await capture(page, "REPEAT_MORNING_SEPARATED.png"));
  return { seeded };
}

function writeReport(summary) {
  const lines = [];
  lines.push("# Current Scenario Advanced Playwright Report");
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
    const details = result.details || {};
    if (details.seeded) {
      lines.push(`- Pattern: ${details.seeded.pattern_code || "-"}`);
      lines.push(`- Tone: ${details.seeded.tone_key || "-"}`);
      lines.push(`- Message: ${details.seeded.chat_content || "-"}`);
      lines.push(`- Flags: ${JSON.stringify(details.seeded.missed_dose_flags || [])}`);
    }
    if (details.before && details.after) {
      lines.push(`- Time changed: ${details.before} -> ${details.after}`);
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
    await runCase(page, "TIME_BAR_LIVE_UPDATE", "상단 simulation time이 polling으로 갱신된다", (screenshots) => runTimeBarLiveUpdate(page, screenshots));
    await runCase(page, "FLAG_CLEARED_CHAT_DETACHED", "아침 미복용 flag가 점심 복용 후 해제되고 stale chat 답변에 붙지 않는다", (screenshots) =>
      runFlagClearedAndChatDetached(page, screenshots),
    );
    await runCase(page, "REPEAT_MORNING_SEPARATED", "다음날 아침 반복은 하루 전체 문제가 아니라 아침 slot 문제로 분리된다", (screenshots) =>
      runRepeatMorningSeparated(page, screenshots),
    );
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

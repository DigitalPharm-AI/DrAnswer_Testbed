const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8100";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const INTERNAL_API_TOKEN = process.env.INTERNAL_API_TOKEN || "playwright-internal-token";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `async-agent-flow-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });
const results = [];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function attr(tag, name) {
  const match = tag.match(new RegExp(`${name}=(['"])([\\s\\S]*?)\\1`));
  return match ? match[2].replace(/&quot;/g, '"').replace(/&#34;/g, '"').replace(/&#39;/g, "'") : "";
}

function formValues(formHtml) {
  const values = {};
  for (const input of formHtml.matchAll(/<input\b[^>]*>/g)) {
    const tag = input[0];
    const name = attr(tag, "name");
    if (name) {
      values[name] = attr(tag, "value");
    }
  }
  const button = formHtml.match(/<button\b[^>]*>([\s\S]*?)<\/button>/);
  values.buttonText = button ? plainText(button[1]) : "";
  return values;
}

function extractForms(html, action) {
  const escaped = action.replace(/\//g, "\\/");
  const regex = new RegExp(`<form\\b[^>]*action="${escaped}"[\\s\\S]*?<\\/form>`, "g");
  return [...html.matchAll(regex)].map((match) => formValues(match[0]));
}

async function getText(pathname) {
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

async function postJson(pathname, payload) {
  const response = await fetch(`${BASE_URL}${pathname}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Internal-Api-Token": INTERNAL_API_TOKEN,
    },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(async () => ({ text: await response.text() }));
  return { status: response.status, body };
}

async function waitFor(name, predicate, timeoutMs = 120000, intervalMs = 1000) {
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
    await sleep(intervalMs);
  }
  throw new Error(`timeout:${name}${lastError ? ` last=${lastError}` : ""}`);
}

async function chatInfo() {
  const html = await getText("/partials/chat-log");
  const countMatch = html.match(/data-chat-message-count="(\d+)"/);
  return {
    count: countMatch ? Number(countMatch[1]) : 0,
    html,
    text: plainText(html),
  };
}

async function feed(afterId = 0) {
  return getJson(`/api/notifications/feed?after_id=${afterId}`);
}

function maxNotificationId(payload) {
  return (payload.notifications || []).reduce((max, row) => Math.max(max, Number(row.id || 0)), 0);
}

async function setupMedication() {
  await postForm("/simulation/reset");
  await postForm("/reminders/suppression", { suppressed: "false" });
  const payload = await feed(0);
  const currentDate = payload.current_time.slice(0, 10);
  await postForm("/medications", {
    medication_choice: "당뇨약",
    dosage_choice: "1정",
    start_date: currentDate,
    end_date: currentDate,
    schedule_template: "morning_evening",
    instructions: `async agent flow ${RUN_ID}`,
  });
  await postForm("/phr/register");
  return { currentDate, currentTime: payload.current_time };
}

async function gotoChat(page, name) {
  await page.goto(`${BASE_URL}/?asyncFlow=${encodeURIComponent(name)}&run=${RUN_ID}#chat`, {
    waitUntil: "domcontentloaded",
    timeout: 20000,
  });
  await page.waitForSelector("#chat-panel", { timeout: 10000 });
}

async function capture(page, name) {
  await gotoChat(page, name);
  const fileName = `${name}.png`;
  await page.screenshot({ path: path.join(OUT_DIR, fileName), fullPage: true });
  return fileName;
}

async function runCase(page, id, title, fn) {
  const started = Date.now();
  const screenshots = [];
  try {
    const details = await fn(page, screenshots);
    results.push({ id, title, status: "passed", elapsedMs: Date.now() - started, details, screenshots });
    console.log(`PASS ${id} ${Date.now() - started}ms`);
  } catch (error) {
    const screenshot = `${id}-failed.png`;
    try {
      await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
      screenshots.push(screenshot);
    } catch (_) {
      // Best effort only.
    }
    results.push({ id, title, status: "failed", elapsedMs: Date.now() - started, error: error.message || String(error), screenshots });
    console.error(`FAIL ${id} ${Date.now() - started}ms ${error.message || error}`);
  }
}

async function runGeneralChat(page, screenshots) {
  const before = await chatInfo();
  const marker = `일반 대화 확인 ${RUN_ID}`;
  await postForm("/chat/system", { event_type: "multiturn_chat", message: marker });
  const after = await waitFor("general_chat_sync_answer", async () => {
    const info = await chatInfo();
    if (info.text.includes("AI 에이전트 오류")) {
      throw new Error(info.text.slice(-500));
    }
    return info.count >= before.count + 2 && info.text.includes(marker) && info.text.includes("말씀을 확인했습니다") ? info : null;
  });
  screenshots.push(await capture(page, "01_general_chat_sync"));
  return { beforeCount: before.count, afterCount: after.count };
}

async function runSideEffectContinuation(page, screenshots) {
  const before = await chatInfo();
  await postForm("/chat/system", { event_type: "multiturn_chat", message: "속이 메스꺼운데 약 때문일까?" });
  const ack = await waitFor("side_effect_immediate_ack", async () => {
    const info = await chatInfo();
    return info.count >= before.count + 2 && info.text.includes("증상 내용을 확인해서 문항을 준비할게요") ? info : null;
  });
  const ae = await waitFor("side_effect_async_ae_prompt", async () => {
    const info = await chatInfo();
    const forms = extractForms(info.html, "/chat/ae-response");
    return info.text.includes("PRO-CTCAE") && forms.length > 0 ? { info, forms } : null;
  });
  screenshots.push(await capture(page, "02_side_effect_async_ctcae"));
  return { ackCount: ack.count, aeFormCount: ae.forms.length };
}

async function runPolicyContinuation(page, screenshots) {
  const before = await chatInfo();
  await postForm("/chat/system", { event_type: "multiturn_chat", message: "아침 알림을 2번 10분 간격으로 바꿔줘" });
  const ack = await waitFor("policy_immediate_ack", async () => {
    const info = await chatInfo();
    return info.count >= before.count + 2 && info.text.includes("알림 정책 변경 후보를 만들고 있어요") ? info : null;
  });
  const confirmation = await waitFor("policy_async_confirmation", async () => {
    const info = await chatInfo();
    const forms = extractForms(info.html, "/chat/policy-confirmation");
    return info.text.includes("정책 변경 후보") && forms.length > 0 ? { info, forms } : null;
  });
  screenshots.push(await capture(page, "03_policy_async_confirmation"));
  return { ackCount: ack.count, confirmationFormCount: confirmation.forms.length };
}

async function runManualDailyPatternSubmit(page, screenshots) {
  const before = await chatInfo();
  await postForm("/analysis/run-today");
  await sleep(2500);
  const after = await chatInfo();
  assert(!after.text.includes("AI 에이전트 오류"), `manual analysis surfaced agent error:${after.text.slice(-700)}`);
  screenshots.push(await capture(page, "04_manual_daily_pattern_async_submit"));
  return { beforeCount: before.count, afterCount: after.count };
}

async function runAutomaticMissedDoseSubmit(page, screenshots) {
  await setupMedication();
  await postForm("/clock/advance", { minutes: "90" });
  const missed = await waitFor("missed_dose_async_chat_message", async () => {
    const info = await chatInfo();
    if (info.text.includes("AI 에이전트 오류")) {
      throw new Error(info.text.slice(-700));
    }
    return info.text.includes("미복용 대화") || info.text.includes("복약 루틴") || info.text.includes("지금 확인") ? info : null;
  });
  screenshots.push(await capture(page, "05_missed_dose_async_submit"));
  return { chatCount: missed.count };
}

async function runPushCallback(page, screenshots) {
  const beforeFeed = await feed(0);
  const afterId = maxNotificationId(beforeFeed);
  const message = `비동기 push callback ${RUN_ID}`;
  const response = await postJson("/api/agent/async/push-messages", {
    request_id: `playwright-push-${RUN_ID}`,
    type: "async_test_push",
    title: "비동기 테스트 알림",
    message,
    sendTime: beforeFeed.current_time,
    metadata: { category: "playwright_async_agent_flow" },
  });
  assert(response.status === 200, `push callback failed:${JSON.stringify(response)}`);
  const pushed = await waitFor("push_message_visible_in_feed", async () => {
    const payload = await feed(afterId);
    return (payload.notifications || []).some((row) => row.body === message) ? payload : null;
  });
  await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded", timeout: 20000 });
  screenshots.push(await capture(page, "06_push_message_feed"));
  return { afterId, receivedCount: (pushed.notifications || []).length };
}

function writeReport() {
  const lines = [];
  lines.push("# Async Agent Flow Playwright Report");
  lines.push("");
  lines.push(`- Base URL: ${BASE_URL}`);
  lines.push(`- Run ID: ${RUN_ID}`);
  lines.push("");
  lines.push("| Scenario | Status | Elapsed |");
  lines.push("|---|---:|---:|");
  for (const result of results) {
    lines.push(`| ${result.id} ${result.title} | ${result.status} | ${result.elapsedMs}ms |`);
  }
  lines.push("");
  lines.push("```json");
  lines.push(JSON.stringify(results, null, 2));
  lines.push("```");
  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
}

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
  try {
    await setupMedication();
    await runCase(page, "01", "일반 멀티턴 채팅은 동기 응답으로 남는다", runGeneralChat);
    await runCase(page, "02", "부작용 질문은 즉시 ack 후 CTCAE 문항이 비동기로 붙는다", runSideEffectContinuation);
    await runCase(page, "03", "정책 변경 요청은 즉시 ack 후 confirmation이 비동기로 붙는다", runPolicyContinuation);
    await runCase(page, "04", "수동 daily pattern 분석은 async submit만 수행한다", runManualDailyPatternSubmit);
    await runCase(page, "05", "자동 미복용 처리는 async submit 후 채팅 메시지를 만든다", runAutomaticMissedDoseSubmit);
    await runCase(page, "06", "push-messages callback은 sendTime 기준으로 feed에 표시된다", runPushCallback);
  } finally {
    await browser.close();
    writeReport();
  }
  const failed = results.filter((result) => result.status !== "passed");
  if (failed.length) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

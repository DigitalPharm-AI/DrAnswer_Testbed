const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `bug-hunt-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

const results = [];

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
  const match = tag.match(new RegExp(`${name}=['"]([^'"]*)['"]`));
  return match ? match[1] : "";
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

async function waitFor(name, predicate, timeoutMs = 90000, intervalMs = 1000) {
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

async function chatInfo() {
  const html = await get("/partials/chat-log");
  const countMatch = html.match(/data-chat-message-count="(\d+)"/);
  return {
    count: countMatch ? Number(countMatch[1]) : 0,
    text: plainText(html),
    html,
  };
}

function extractForms(html, action) {
  const escaped = action.replace(/\//g, "\\/");
  const regex = new RegExp(`<form\\b[^>]*action="${escaped}"[\\s\\S]*?<\\/form>`, "g");
  return [...html.matchAll(regex)].map((match) => formValues(match[0]));
}

async function extractAeResponseForms() {
  return extractForms((await chatInfo()).html, "/chat/ae-response");
}

async function extractSafetyForms() {
  return extractForms((await chatInfo()).html, "/chat/side-effect-reminder-safety");
}

function pickAeAnswer(forms, questionIndex, preferredTexts) {
  const candidates = forms.filter((form) => String(form.question_index) === String(questionIndex));
  return candidates.find((form) => preferredTexts.some((text) => form.response_text === text || form.buttonText === text)) || candidates[0] || null;
}

async function feed(afterId = 0) {
  return getJson(`/api/notifications/feed?after_id=${afterId}`);
}

function maxNotificationId(payload) {
  const rows = payload.notifications || [];
  return rows.reduce((max, row) => Math.max(max, Number(row.id || 0)), 0);
}

async function setupMedication() {
  await postForm("/simulation/reset");
  const payload = await feed(0);
  const currentDate = payload.current_time.slice(0, 10);
  await postForm("/medications", {
    medication_choice: "당뇨약",
    dosage_choice: "1정",
    start_date: currentDate,
    end_date: currentDate,
    schedule_template: "morning_evening",
    instructions: "playwright bug hunt",
  });
  await postForm("/phr/register");
}

async function triggerSideEffectPrompt() {
  const before = await chatInfo();
  await postForm("/chat/system", { event_type: "multiturn_chat", message: "속이 메스꺼운데 약 때문일까?" });
  await waitFor(
    "side_effect_agent_response_with_ae_forms",
    async () => {
      const info = await chatInfo();
      if (info.text.includes("AI 에이전트 오류") || info.text.includes("AI가 대화를 처리하지 못했습니다")) {
        throw new Error(`agent_error_in_chat:${info.text.slice(-500)}`);
      }
      if (info.text.includes("응답 생성 중") || info.count < before.count + 2) {
        return false;
      }
      return info.text.includes("PRO-CTCAE") && extractForms(info.html, "/chat/ae-response").length > 0;
    },
    150000,
    1500,
  );
}

async function answerSideEffectSurvey() {
  let forms = await extractAeResponseForms();
  const first = pickAeAnswer(forms, 0, ["자주 있다", "가끔 있다", "거의 항상 있다"]);
  if (!first) {
    throw new Error(`ae_first_question_not_found:${JSON.stringify(forms)}`);
  }
  await postForm("/chat/ae-response", {
    chat_message_id: first.chat_message_id,
    question_index: first.question_index,
    response_text: first.response_text || first.buttonText,
  });

  forms = await extractAeResponseForms();
  const second = pickAeAnswer(forms, 1, ["보통이다", "심하다", "약간 있다"]);
  if (!second) {
    throw new Error(`ae_second_question_not_found:${JSON.stringify(forms)}`);
  }
  await postForm("/chat/ae-response", {
    chat_message_id: second.chat_message_id,
    question_index: second.question_index,
    response_text: second.response_text || second.buttonText,
  });

  await waitFor(
    "side_effect_safety_prompt",
    async () => {
      const info = await chatInfo();
      return info.text.includes("부작용에 대해 기록했습니다") && extractForms(info.html, "/chat/side-effect-reminder-safety").length >= 2;
    },
    30000,
    1000,
  );
}

async function chooseLatestSafetyAction(action) {
  const forms = await extractSafetyForms();
  const selected = forms
    .filter((form) => form.action === action)
    .sort((left, right) => Number(right.notification_id || 0) - Number(left.notification_id || 0))[0];
  if (!selected) {
    throw new Error(`safety_action_form_not_found:${action}:${JSON.stringify(forms)}`);
  }
  await postForm("/chat/side-effect-reminder-safety", {
    notification_id: selected.notification_id,
    action: selected.action,
  });
  return selected.notification_id;
}

async function runProbe(page, name, severity, fn) {
  const started = Date.now();
  try {
    const details = await fn();
    const elapsedMs = Date.now() - started;
    results.push({ name, severity, status: "passed", elapsedMs, details });
    console.log(`PASS ${name} ${elapsedMs}ms`);
  } catch (error) {
    const elapsedMs = Date.now() - started;
    const screenshot = path.join(OUT_DIR, `${results.length + 1}-${name.replace(/[^a-z0-9_-]+/gi, "_")}.png`);
    try {
      await page.screenshot({ path: screenshot, fullPage: true });
    } catch (_) {
      // Best effort only.
    }
    results.push({ name, severity, status: "failed", elapsedMs, error: error.message || String(error), screenshot });
    console.error(`FAIL ${name} ${elapsedMs}ms ${error.message || error}`);
  }
}

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1200 } });
  const page = await context.newPage();
  const pageErrors = [];
  const failedRequests = [];
  const consoleErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()} ${request.failure()?.errorText || ""}`.trim());
  });

  await runProbe(page, "01_ui_health_console_and_overflow_desktop_mobile", "medium", async () => {
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForSelector("text=복약 알림 + AI 에이전트 시뮬레이터", { timeout: 10000 });
    const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    await page.screenshot({ path: path.join(OUT_DIR, "01-desktop.png"), fullPage: true });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForSelector("text=복약 알림 + AI 에이전트 시뮬레이터", { timeout: 10000 });
    const mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    await page.screenshot({ path: path.join(OUT_DIR, "01-mobile.png"), fullPage: true });

    const relevantFailedRequests = failedRequests.filter(
      (line) =>
        !line.includes("favicon") &&
        !(line.includes("/partials/") && line.includes("ERR_ABORTED")) &&
        !(line.includes("/api/notifications/feed") && line.includes("ERR_ABORTED")),
    );
    if (pageErrors.length || consoleErrors.length || relevantFailedRequests.length) {
      throw new Error(
        JSON.stringify({
          pageErrors,
          consoleErrors,
          failedRequests: relevantFailedRequests,
        }),
      );
    }
    if (desktopOverflow > 2 || mobileOverflow > 2) {
      throw new Error(`horizontal_overflow desktop=${desktopOverflow} mobile=${mobileOverflow}`);
    }
    return { desktopOverflow, mobileOverflow };
  });

  await runProbe(page, "02_chat_tab_click_refreshes_server_side_new_message", "high", async () => {
    await postForm("/simulation/reset");
    await page.setViewportSize({ width: 1440, height: 1200 });
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
    const unique = `chat-refresh-probe-${RUN_ID}`;
    await postForm("/chat/system", { event_type: "multiturn_chat", message: unique });
    await page.click('.app-tab[data-tab-target="chat-page"]');
    await page.waitForSelector("#chat-panel", { timeout: 10000 });
    await waitFor(
      "chat_refresh_unique_message",
      async () => {
        const text = await page.locator("#chat-panel").innerText();
        return text.includes(unique);
      },
      15000,
      500,
    );
    await page.screenshot({ path: path.join(OUT_DIR, "02-chat-refresh.png"), fullPage: true });
    return { unique };
  });

  await runProbe(page, "03_chat_composer_enter_sends_alt_enter_inserts_newline", "medium", async () => {
    await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForSelector("#agent-chat-message", { timeout: 10000 });
    const unique = `keyboard-probe-${RUN_ID}`;
    await page.fill("#agent-chat-message", `${unique}-line1`);
    await page.keyboard.down("Alt");
    await page.keyboard.press("Enter");
    await page.keyboard.up("Alt");
    await page.keyboard.type(`${unique}-line2`);
    const valueBeforeSubmit = await page.locator("#agent-chat-message").inputValue();
    if (!valueBeforeSubmit.includes("\n")) {
      throw new Error(`alt_enter_did_not_insert_newline:${JSON.stringify(valueBeforeSubmit)}`);
    }
    await page.keyboard.press("Enter");
    await waitFor(
      "keyboard_probe_user_message",
      async () => {
        const text = await page.locator("#chat-panel").innerText();
        return text.includes(`${unique}-line1`) && text.includes(`${unique}-line2`);
      },
      20000,
      500,
    );
    await page.screenshot({ path: path.join(OUT_DIR, "03-keyboard.png"), fullPage: true });
    return { valueBeforeSubmit };
  });

  let completedSafetyNotificationId = "";
  await runProbe(page, "04_side_effect_flow_creates_safety_prompt_and_suppress_reply", "high", async () => {
    await setupMedication();
    await triggerSideEffectPrompt();
    await answerSideEffectSurvey();
    completedSafetyNotificationId = await chooseLatestSafetyAction("suppress");
    await waitFor(
      "suppress_reply_visible",
      async () => {
        const info = await chatInfo();
        return info.text.includes("복약 알림과 미복용 AI 알림을 모두 껐습니다");
      },
      20000,
      1000,
    );
    await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.screenshot({ path: path.join(OUT_DIR, "04-side-effect-suppress.png"), fullPage: true });
    return { completedSafetyNotificationId };
  });

  await runProbe(page, "05_completed_side_effect_prompt_has_no_active_buttons", "high", async () => {
    const info = await chatInfo();
    const safetyForms = extractForms(info.html, "/chat/side-effect-reminder-safety");
    const staleForms = safetyForms.filter((form) => String(form.notification_id) === String(completedSafetyNotificationId));
    if (staleForms.length) {
      throw new Error(`stale_completed_safety_forms_remain:${JSON.stringify(staleForms)}`);
    }
    return { safetyForms: safetyForms.length };
  });

  await runProbe(page, "06_duplicate_completed_safety_click_gives_visible_feedback", "medium", async () => {
    if (!completedSafetyNotificationId) {
      throw new Error("missing_completedSafetyNotificationId");
    }
    const before = await chatInfo();
    await postForm("/chat/side-effect-reminder-safety", {
      notification_id: completedSafetyNotificationId,
      action: "keep",
    });
    const after = await chatInfo();
    const gainedMessage = after.count > before.count;
    const hasAlreadyProcessedText = after.text.includes("이미 처리된 알림입니다");
    if (!gainedMessage && !hasAlreadyProcessedText) {
      throw new Error(`duplicate_completed_safety_action_has_no_visible_feedback before=${before.count} after=${after.count}`);
    }
    return { beforeCount: before.count, afterCount: after.count, hasAlreadyProcessedText };
  });

  await browser.close();

  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    outputDir: OUT_DIR,
    results,
    passed: results.filter((result) => result.status === "passed").length,
    failed: results.filter((result) => result.status === "failed").length,
    findings: results.filter((result) => result.status === "failed"),
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  console.log(JSON.stringify(summary, null, 2));
  if (summary.failed > 0) {
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

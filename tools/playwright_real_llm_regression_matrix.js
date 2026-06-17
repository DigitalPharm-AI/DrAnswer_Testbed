const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const INTERNAL_API_TOKEN = process.env.INTERNAL_API_TOKEN || "playwright-internal-token";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `real-llm-regression-${RUN_ID}`);
const VIDEO_DIR = path.join(OUT_DIR, "videos");

fs.mkdirSync(OUT_DIR, { recursive: true });
fs.mkdirSync(VIDEO_DIR, { recursive: true });

const results = [];

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

function decodeHtml(value) {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&#34;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function attr(tag, name) {
  const match = tag.match(new RegExp(`${name}=(['"])([\\s\\S]*?)\\1`));
  return match ? decodeHtml(match[2]) : "";
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
  let body = {};
  try {
    body = await response.json();
  } catch (_) {
    body = { text: await response.text() };
  }
  return { status: response.status, body };
}

async function waitFor(name, predicate, timeoutMs = 180000, intervalMs = 1500) {
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

async function setupMedication({ timesCsv = "08:00", endDate = null } = {}) {
  await postForm("/simulation/reset");
  await postForm("/reminders/suppression", { suppressed: "false" });
  const payload = await feed(0);
  const currentDate = payload.current_time.slice(0, 10);
  await postForm("/medications", {
    medication_choice: "당뇨약",
    dosage_choice: "1정",
    start_date: currentDate,
    end_date: endDate || currentDate,
    schedule_template: "custom",
    times_csv: timesCsv,
    instructions: `real LLM regression ${RUN_ID}`,
  });
  await postForm("/phr/register");
  return { currentDate };
}

function assertNoAgentError(text) {
  if (text.includes("AI 에이전트 오류") || text.includes("AI가 대화를 처리하지 못했습니다") || text.includes("agent_error")) {
    throw new Error(`agent_error_visible:${text.slice(-700)}`);
  }
}

async function chooseLatestPolicyAction(action) {
  const info = await chatInfo();
  const forms = extractForms(info.html, "/chat/policy-confirmation");
  const selected = forms
    .filter((form) => {
      try {
        const message = JSON.parse(form.message || "{}");
        return message.action === action;
      } catch (_) {
        return false;
      }
    })
    .sort((left, right) => Number(right.notification_id || 0) - Number(left.notification_id || 0))[0];
  if (!selected) {
    throw new Error(`policy_action_form_not_found:${action}:${JSON.stringify(forms)}`);
  }
  await postForm("/chat/policy-confirmation", {
    notification_id: selected.notification_id,
    message: selected.message,
  });
  return selected.notification_id;
}

async function chooseLatestSideEffectSafetyAction(action) {
  const info = await chatInfo();
  const forms = extractForms(info.html, "/chat/side-effect-reminder-safety");
  const selected = forms
    .filter((form) => form.action === action)
    .sort((left, right) => Number(right.notification_id || 0) - Number(left.notification_id || 0))[0];
  if (!selected) {
    throw new Error(`side_effect_safety_action_form_not_found:${action}:${JSON.stringify(forms)}`);
  }
  await postForm("/chat/side-effect-reminder-safety", {
    notification_id: selected.notification_id,
    action: selected.action,
  });
  return selected.notification_id;
}

function pickAeAnswer(forms, questionIndex, preferredTexts) {
  const candidates = forms.filter((form) => String(form.question_index) === String(questionIndex));
  return candidates.find((form) => preferredTexts.some((text) => form.response_text === text || form.buttonText === text)) || candidates[0] || null;
}

async function answerFirstTwoAeQuestions() {
  let forms = extractForms((await chatInfo()).html, "/chat/ae-response");
  const first = pickAeAnswer(forms, 0, ["자주 있다", "가끔 있다", "거의 항상 있다"]);
  if (!first) {
    throw new Error(`ae_first_question_not_found:${JSON.stringify(forms)}`);
  }
  await postForm("/chat/ae-response", {
    chat_message_id: first.chat_message_id,
    question_index: first.question_index,
    response_text: first.response_text || first.buttonText,
  });
  forms = extractForms((await chatInfo()).html, "/chat/ae-response");
  const second = pickAeAnswer(forms, 1, ["보통이다", "심하다", "약간 있다"]);
  if (!second) {
    throw new Error(`ae_second_question_not_found:${JSON.stringify(forms)}`);
  }
  await postForm("/chat/ae-response", {
    chat_message_id: second.chat_message_id,
    question_index: second.question_index,
    response_text: second.response_text || second.buttonText,
  });
}

async function runScenario(page, id, title, fn) {
  const started = Date.now();
  console.log(`SCENARIO ${id} start`);
  try {
    const details = await fn();
    const elapsedMs = Date.now() - started;
    results.push({ id, title, status: "passed", elapsedMs, details });
    console.log(`PASS ${id} ${elapsedMs}ms`);
  } catch (error) {
    const elapsedMs = Date.now() - started;
    const screenshot = `${id}-failed.png`;
    try {
      await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
    } catch (_) {
      // Best effort.
    }
    results.push({ id, title, status: "failed", elapsedMs, error: error.message || String(error), screenshots: [screenshot] });
    console.error(`FAIL ${id} ${elapsedMs}ms ${error.message || error}`);
  }
}

function writeReport(summary) {
  const lines = [];
  lines.push("# Real LLM Regression Matrix Playwright Report");
  lines.push("");
  lines.push(`- Run ID: ${summary.runId}`);
  lines.push(`- Target URL: ${summary.baseUrl}`);
  lines.push(`- Output directory: ${summary.outputDir}`);
  if (summary.videos.length > 0) {
    lines.push(`- Video: ${summary.videos.join(", ")}`);
  }
  lines.push("- Scope: real browser + real agent_app provider for LLM-dependent paths, plus safety guard checks");
  lines.push("");
  lines.push("## Results");
  lines.push("");
  lines.push("| Scenario | Status | Elapsed ms | Evidence |");
  lines.push("|---|---:|---:|---|");
  for (const result of summary.results) {
    const evidence = result.status === "passed" ? JSON.stringify(result.details || {}).replace(/\|/g, "\\|") : result.error;
    lines.push(`| ${result.title.replace(/\|/g, "\\|")} | ${result.status} | ${result.elapsedMs} | ${evidence} |`);
  }
  lines.push("");
  lines.push(`- Passed: ${summary.passed}`);
  lines.push(`- Failed: ${summary.failed}`);
  if (summary.videos.length > 0) {
    lines.push("");
    lines.push("## Videos");
    lines.push("");
    for (const video of summary.videos) {
      lines.push(`- ${video}`);
    }
  }
  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
}

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1200 },
    recordVideo: {
      dir: VIDEO_DIR,
      size: { width: 1440, height: 1200 },
    },
  });
  const page = await context.newPage();
  let videoPath = "";

  try {
    await runScenario(page, "01_multiturn_recall", "일반 멀티턴 회상이 fallback 없이 앞선 발화를 회상함", async () => {
      await setupMedication({ timesCsv: "08:00" });
      await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
      await postForm("/chat/system", {
        event_type: "multiturn_chat",
        message: "내가 좋아하는 색은 파란색이라고 기억해줘.",
      });
      await waitFor("first_general_chat_answer", async () => {
        const info = await chatInfo();
        assertNoAgentError(info.text);
        return info.count >= 2;
      });
      await postForm("/chat/system", {
        event_type: "multiturn_chat",
        message: "아까 내가 뭐라고 했지?",
      });
      const recall = await waitFor("recall_answer_mentions_prior_turn", async () => {
        const info = await chatInfo();
        assertNoAgentError(info.text);
        if (info.text.includes("요청을 확인했습니다.")) {
          throw new Error(`fallback_reply_visible:${info.text.slice(-600)}`);
        }
        return info.text.includes("파란") ? info : false;
      });
      await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
      await page.screenshot({ path: path.join(OUT_DIR, "01-recall.png"), fullPage: true });
      return { chatCount: recall.count, containsPriorMemory: true };
    });

    await runScenario(page, "02_side_effect_suppress_and_reenable", "부작용 기록 후 알림 전체 끄기와 UI 켜기가 동작함", async () => {
      await setupMedication({ timesCsv: "08:00" });
      await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
      await postForm("/chat/system", {
        event_type: "multiturn_chat",
        message: "속이 메스꺼운데 당뇨약 때문일까? 필요한 부작용 평가 문항이 있으면 진행해줘.",
      });
      await waitFor("ae_forms_for_suppression", async () => {
        const info = await chatInfo();
        assertNoAgentError(info.text);
        const forms = extractForms(info.html, "/chat/ae-response");
        return info.text.includes("PRO-CTCAE") && forms.length > 0 ? true : false;
      });
      await answerFirstTwoAeQuestions();
      await waitFor("safety_prompt_for_suppression", async () => {
        const info = await chatInfo();
        return info.text.includes("부작용에 대해 기록했습니다") && extractForms(info.html, "/chat/side-effect-reminder-safety").length >= 2;
      }, 30000, 1000);
      const suppressNotificationId = await chooseLatestSideEffectSafetyAction("suppress");
      await waitFor("suppressed_policy_panel", async () => {
        const text = plainText(await getText("/partials/active-policies"));
        return text.includes("알림 전체 켜기") && text.includes("수동 패턴 분석이 중지되어 있습니다.");
      }, 30000, 1000);
      await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
      const manualAnalysisDisabled = await page.locator('form[action="/analysis/run-today"] button').isDisabled();
      if (!manualAnalysisDisabled) {
        throw new Error("manual_analysis_button_not_disabled_when_suppressed");
      }
      await postForm("/reminders/suppression", { suppressed: "false" });
      await waitFor("reenabled_policy_panel", async () => {
        const text = plainText(await getText("/partials/active-policies"));
        return text.includes("알림 전체 끄기") && text.includes("수동 패턴 분석이 활성화되어 있습니다.");
      }, 30000, 1000);
      await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
      await page.screenshot({ path: path.join(OUT_DIR, "02-suppress-reenable.png"), fullPage: true });
      return { suppressNotificationId };
    });

    await runScenario(page, "03_system_policy_confirmation", "시스템 정책 변경도 직접 적용 없이 confirmation 후 적용됨", async () => {
      await setupMedication({ timesCsv: "08:00" });
      const beforeText = plainText(await getText("/partials/active-policies"));
      if (beforeText.includes("일일 패턴 대화 요청 시간: 07:00")) {
        throw new Error(`system_policy_already_applied_before_request:${beforeText}`);
      }
      await postForm("/chat/system", {
        event_type: "multiturn_chat",
        message: "시스템 운영 정책 daily_pattern_conversation_time 값을 07:00으로 바꿔줘. 바로 적용하지 말고 apply_system_policy 도구로 확인 후보를 만들어줘.",
      });
      await waitFor("system_policy_confirmation_from_llm", async () => {
        const info = await chatInfo();
        assertNoAgentError(info.text);
        const forms = extractForms(info.html, "/chat/policy-confirmation");
        return info.text.includes("일일 패턴 대화 요청 시간") && forms.length >= 2 ? { info, forms } : false;
      }, 180000, 1500);
      const activeBeforeChoice = plainText(await getText("/partials/active-policies"));
      if (activeBeforeChoice.includes("일일 패턴 대화 요청 시간: 07:00")) {
        throw new Error(`system_policy_applied_before_confirmation:${activeBeforeChoice}`);
      }
      const notificationId = await chooseLatestPolicyAction("increase");
      await waitFor("system_policy_applied_after_confirmation", async () => {
        const text = plainText(await getText("/partials/active-policies"));
        return text.includes("일일 패턴 대화 요청 시간: 07:00");
      }, 30000, 1000);
      await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
      await page.screenshot({ path: path.join(OUT_DIR, "03-system-policy.png"), fullPage: true });
      return { notificationId };
    });

    await runScenario(page, "04_direct_apply_endpoints_rejected", "정책 직접 적용 endpoint는 내부 토큰이 있어도 410으로 거부됨", async () => {
      await setupMedication({ timesCsv: "08:00" });
      const notificationPayload = {
        idempotency_key: `real-llm-regression-policy-${RUN_ID}`,
        source_trace_id: "trace-direct-policy",
        source_event_type: "daily_pattern",
        policies: [
          {
            slot_label: "아침 08:00",
            extra_reminders: 2,
            interval_minutes: 10,
            effective_start_date: "2026-04-20",
            effective_end_date: "2026-04-27",
            reason: "direct apply should be rejected",
            source: "pattern_analysis",
          },
        ],
      };
      const systemPayload = {
        idempotency_key: `real-llm-regression-system-${RUN_ID}`,
        source_trace_id: "trace-direct-system-policy",
        source_event_type: "multiturn_chat",
        policies: [
          {
            policy_key: "daily_pattern_conversation_time",
            value: "07:00",
            reason: "direct system apply should be rejected",
          },
        ],
      };
      const notificationResponse = await postJson("/api/agent/policies/apply", notificationPayload);
      const systemResponse = await postJson("/api/agent/system-policies/apply", systemPayload);
      if (notificationResponse.status !== 410 || notificationResponse.body.detail !== "policy_apply_requires_confirmation") {
        throw new Error(`notification_direct_apply_not_rejected:${JSON.stringify(notificationResponse)}`);
      }
      if (systemResponse.status !== 410 || systemResponse.body.detail !== "system_policy_apply_requires_confirmation") {
        throw new Error(`system_direct_apply_not_rejected:${JSON.stringify(systemResponse)}`);
      }
      const activeText = plainText(await getText("/partials/active-policies"));
      if (activeText.includes("맞춤 정책") || activeText.includes("일일 패턴 대화 요청 시간: 07:00")) {
        throw new Error(`direct_apply_changed_policy:${activeText}`);
      }
      return { notificationStatus: notificationResponse.status, systemStatus: systemResponse.status };
    });

    await runScenario(page, "05_suppression_blocks_automatic_alerts", "알림 전체 꺼짐 상태에서는 자동 복약/미복용 알림이 생성되지 않음", async () => {
      await setupMedication({ timesCsv: "08:00" });
      await postForm("/reminders/suppression", { suppressed: "true" });
      await postForm("/clock/advance", { minutes: "180" });
      await sleep(1500);
      const payload = await feed(0);
      const alerts = (payload.notifications || []).filter((row) => ["medication_alert", "conversation_alert"].includes(row.notification_type));
      if (alerts.length > 0) {
        throw new Error(`alerts_created_while_suppressed:${JSON.stringify(alerts)}`);
      }
      const activeText = plainText(await getText("/partials/active-policies"));
      if (!activeText.includes("알림 전체 켜기")) {
        throw new Error(`suppression_not_visible:${activeText}`);
      }
      await postForm("/reminders/suppression", { suppressed: "false" });
      return { alertCount: alerts.length };
    });

    await runScenario(page, "06_manual_analysis_suppressed", "알림 전체 꺼짐 상태에서는 수동 daily pattern 분석도 실행되지 않음", async () => {
      await setupMedication({ timesCsv: "08:00" });
      await postForm("/reminders/suppression", { suppressed: "true" });
      await postForm("/analysis/run-today");
      await sleep(1500);
      const info = await chatInfo();
      const payload = await feed(0);
      const policyAlerts = (payload.notifications || []).filter((row) => row.metadata && row.metadata.category === "policy_confirmation");
      if (policyAlerts.length > 0 || info.text.includes("정책 변경 후보")) {
        throw new Error(`manual_analysis_created_policy_while_suppressed:${JSON.stringify(policyAlerts)}:${info.text}`);
      }
      await postForm("/reminders/suppression", { suppressed: "false" });
      return { policyAlertCount: policyAlerts.length };
    });
  } finally {
    const video = page.video();
    await context.close();
    if (video) {
      videoPath = await video.path();
    }
    await browser.close();
  }

  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    outputDir: OUT_DIR,
    videos: videoPath ? [path.relative(process.cwd(), videoPath)] : [],
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

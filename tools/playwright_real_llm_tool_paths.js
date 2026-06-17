const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `real-llm-tools-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

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
  const payload = await feed(0);
  const currentDate = payload.current_time.slice(0, 10);
  await postForm("/medications", {
    medication_choice: "당뇨약",
    dosage_choice: "1정",
    start_date: currentDate,
    end_date: endDate || currentDate,
    schedule_template: "custom",
    times_csv: timesCsv,
    instructions: `real LLM tool path ${RUN_ID}`,
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

function pickAeAnswer(forms, questionIndex, preferredTexts) {
  const candidates = forms.filter((form) => String(form.question_index) === String(questionIndex));
  return candidates.find((form) => preferredTexts.some((text) => form.response_text === text || form.buttonText === text)) || candidates[0] || null;
}

async function runScenario(page, id, title, fn) {
  const started = Date.now();
  console.log(`SCENARIO ${id} start`);
  try {
    const details = await fn();
    const elapsedMs = Date.now() - started;
    const result = { id, title, status: "passed", elapsedMs, details };
    results.push(result);
    console.log(`PASS ${id} ${elapsedMs}ms`);
  } catch (error) {
    const elapsedMs = Date.now() - started;
    const screenshot = `${id}-failed.png`;
    try {
      await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
    } catch (_) {
      // Best effort.
    }
    const result = { id, title, status: "failed", elapsedMs, error: error.message || String(error), screenshots: [screenshot] };
    results.push(result);
    console.error(`FAIL ${id} ${elapsedMs}ms ${error.message || error}`);
  }
}

function writeReport(summary) {
  const lines = [];
  lines.push("# Real LLM Tool Path Playwright Report");
  lines.push("");
  lines.push(`- Run ID: ${summary.runId}`);
  lines.push(`- Target URL: ${summary.baseUrl}`);
  lines.push(`- Output directory: ${summary.outputDir}`);
  lines.push("- Provider: actual app configuration, expected `bedrock_anthropic` from environment files");
  lines.push("- Scope: browser + system app + agent app + PHR app + real LLM tool decision path");
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
  lines.push("");
  lines.push("## Notes");
  lines.push("");
  lines.push("- This report intentionally verifies behavior and visible system outcomes, not exact LLM wording.");
  lines.push("- Policy changes are expected to stop at confirmation until the user chooses an option.");
  lines.push("- Side-effect causality should go through lookup/pro-ctcae behavior before the deterministic safety prompt appears.");
  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
}

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

  await runScenario(page, "01_policy_request_tool_confirmation", "멀티턴 정책 요청이 LLM tool_call 후 confirmation으로 멈춤", async () => {
    await setupMedication({ timesCsv: "08:00" });
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
    const beforeActivePolicies = plainText(await getText("/partials/active-policies"));
    if (beforeActivePolicies.includes("맞춤 정책")) {
      throw new Error(`custom_policy_exists_before_request:${beforeActivePolicies}`);
    }
    await postForm("/chat/system", {
      event_type: "multiturn_chat",
      message: "아침 08:00 당뇨약 복약 알림을 추가 2회, 10분 간격으로 늘려줘. 정책 후보를 만들고 확인을 받아줘.",
    });
    const policyInfo = await waitFor("policy_confirmation_from_real_llm", async () => {
      const info = await chatInfo();
      assertNoAgentError(info.text);
      const forms = extractForms(info.html, "/chat/policy-confirmation");
      return forms.length > 0 ? { info, forms } : false;
    });
    const activeBeforeChoice = plainText(await getText("/partials/active-policies"));
    if (activeBeforeChoice.includes("맞춤 정책")) {
      throw new Error(`policy_applied_before_confirmation:${activeBeforeChoice}`);
    }
    const notificationId = await chooseLatestPolicyAction("increase");
    await waitFor("policy_applied_after_confirmation", async () => {
      const text = plainText(await getText("/partials/active-policies"));
      const customPolicyLabel = text.includes("맞춤 정책") || text.includes("요청 정책");
      return text.includes("아침 08:00") && customPolicyLabel && text.includes("2회 추가") && text.includes("10분 간격");
    }, 30000, 1000);
    await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.screenshot({ path: path.join(OUT_DIR, "01-policy-confirmation.png"), fullPage: true });
    return { policyFormCount: policyInfo.forms.length, notificationId };
  });

  await runScenario(page, "02_side_effect_tool_chain", "부작용 질문이 LLM lookup/PRO-CTCAE 경로로 이어짐", async () => {
    await setupMedication({ timesCsv: "08:00" });
    await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await postForm("/chat/system", {
      event_type: "multiturn_chat",
      message: "속이 메스꺼운데 당뇨약 때문일까? 필요한 부작용 평가 문항이 있으면 진행해줘.",
    });
    const aeInfo = await waitFor("ae_forms_from_real_llm", async () => {
      const info = await chatInfo();
      assertNoAgentError(info.text);
      const forms = extractForms(info.html, "/chat/ae-response");
      return info.text.includes("PRO-CTCAE") && forms.length > 0 ? { info, forms } : false;
    });
    let forms = aeInfo.forms;
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
    await waitFor("deterministic_safety_prompt_after_real_llm_ae", async () => {
      const info = await chatInfo();
      const forms = extractForms(info.html, "/chat/side-effect-reminder-safety");
      return info.text.includes("부작용에 대해 기록했습니다") && forms.length >= 2;
    }, 30000, 1000);
    await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.screenshot({ path: path.join(OUT_DIR, "02-side-effect-chain.png"), fullPage: true });
    return { aeInitialFormCount: aeInfo.forms.length };
  });

  await runScenario(page, "03_mark_dose_taken_tool", "복약 완료 보고가 LLM mark_dose_taken tool로 기록됨", async () => {
    await setupMedication({ timesCsv: "08:00" });
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
    const beforeTimeline = plainText(await getText("/partials/timeline"));
    if (!beforeTimeline.includes("상태: scheduled")) {
      throw new Error(`scheduled_dose_not_visible_before:${beforeTimeline}`);
    }
    await postForm("/chat/system", {
      event_type: "multiturn_chat",
      message: "아침 08:00 당뇨약 먹었어. 오늘 복약 완료로 기록해줘.",
    });
    await waitFor("dose_taken_from_real_llm_tool", async () => {
      const chat = await chatInfo();
      assertNoAgentError(chat.text);
      const timeline = plainText(await getText("/partials/timeline"));
      return timeline.includes("상태: taken") && timeline.includes("기록 완료");
    }, 180000, 1500);
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.screenshot({ path: path.join(OUT_DIR, "03-dose-taken.png"), fullPage: true });
    return { timelineStatus: "taken" };
  });

  await browser.close();

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

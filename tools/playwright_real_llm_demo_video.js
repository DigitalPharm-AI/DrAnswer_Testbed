const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const RUN_ID = process.env.DEMO_RUN_ID || String(Date.now());
const OUT_DIR = path.join("outputs", "playwright", `real-llm-demo-${RUN_ID}`);
const VIDEO_DIR = path.join(OUT_DIR, "videos");
const STEP_PAUSE_MS = Number(process.env.DEMO_STEP_PAUSE_MS || 1200);

fs.mkdirSync(OUT_DIR, { recursive: true });
fs.mkdirSync(VIDEO_DIR, { recursive: true });

const steps = [];

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

async function bodyText(page) {
  return page.locator("body").innerText();
}

async function assertNoAgentError(page) {
  const text = await bodyText(page);
  if (text.includes("AI 에이전트 오류") || text.includes("AI가 대화를 처리하지 못했습니다") || text.includes("agent_error")) {
    throw new Error(`agent_error_visible:${text.slice(-900)}`);
  }
}

async function waitForHtmxIdle(page) {
  await page
    .waitForFunction(
      () => !document.querySelector(".htmx-request:not(#time-bar)") && !document.body.classList.contains("htmx-request"),
      { timeout: 15000 },
    )
    .catch(() => {});
  await sleep(350);
}

async function clickAndSettle(page, locator) {
  const navigation = page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 8000 }).catch(() => null);
  await locator.click();
  await navigation;
  await waitForHtmxIdle(page);
}

async function showStep(page, title, detail = "") {
  const label = detail ? `${title}\n${detail}` : title;
  steps.push({ title, detail, at: new Date().toISOString() });
  await page.evaluate((text) => {
    let node = document.getElementById("__demo_caption");
    if (!node) {
      node = document.createElement("div");
      node.id = "__demo_caption";
      Object.assign(node.style, {
        position: "fixed",
        top: "16px",
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: "2147483647",
        maxWidth: "880px",
        whiteSpace: "pre-line",
        padding: "12px 18px",
        borderRadius: "10px",
        background: "rgba(11, 18, 32, 0.92)",
        color: "#fff",
        boxShadow: "0 14px 42px rgba(0, 0, 0, 0.28)",
        font: "600 17px/1.42 system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        textAlign: "center",
        pointerEvents: "none",
      });
      document.body.appendChild(node);
    }
    node.textContent = text;
  }, label);
  await sleep(STEP_PAUSE_MS);
}

async function waitForBodyText(page, expected, timeout = 60000) {
  await page.waitForFunction(
    (needle) => document.body && document.body.innerText.includes(needle),
    expected,
    { timeout },
  );
  await assertNoAgentError(page);
}

async function waitForVisibleText(page, text, timeout = 60000) {
  await page.getByText(text, { exact: false }).first().waitFor({ timeout });
  await assertNoAgentError(page);
}

async function waitForMissedDoseConversationAlert(page, timeout = 180000) {
  await page.waitForFunction(
    () =>
      document.body &&
      document.body.innerText.includes("AI가 대화를 요청합니다") &&
      Boolean(document.querySelector("[data-open-chat]")),
    { timeout },
  );
  await assertNoAgentError(page);
}

async function clickChatTab(page) {
  await clickAndSettle(page, page.getByRole("button", { name: "Chat" }));
  await page.locator("#chat-page.is-active").waitFor({ timeout: 10000 });
}

async function clickHomeTab(page) {
  await clickAndSettle(page, page.getByRole("button", { name: "HOME" }));
  await page.locator("#home-page.is-active").waitFor({ timeout: 10000 });
}

async function sendChat(page, message) {
  await page.locator("#agent-chat-message").fill(message);
  await clickAndSettle(page, page.locator(".chat-composer button[type='submit']"));
}

async function chatMessageCount(page) {
  const value = await page.locator("#chat-log-region").getAttribute("data-chat-message-count").catch(() => "0");
  return Number(value || 0);
}

async function waitForAeOptions(page) {
  await page.locator('form[action="/chat/ae-response"]').first().waitFor({ timeout: 180000 });
  await waitForVisibleText(page, "PRO-CTCAE", 5000);
}

async function clickAeAnswer(page, questionIndex, preferredAnswers) {
  const forms = page.locator('form[action="/chat/ae-response"]');
  const count = await forms.count();
  for (const answer of preferredAnswers) {
    for (let i = 0; i < count; i += 1) {
      const form = forms.nth(i);
      const question = await form.locator('input[name="question_index"]').inputValue().catch(() => "");
      const text = await form.innerText().catch(() => "");
      if (question === String(questionIndex) && text.includes(answer)) {
        await clickAndSettle(page, form.getByRole("button", { name: answer, exact: true }));
        return answer;
      }
    }
  }
  throw new Error(`ae_answer_not_found:${questionIndex}:${preferredAnswers.join(",")}`);
}

async function clickLatestFormButton(page, formAction, preferredNames) {
  const forms = page.locator(`form[action="${formAction}"]`);
  const count = await forms.count();
  for (const name of preferredNames) {
    for (let i = count - 1; i >= 0; i -= 1) {
      const button = forms.nth(i).getByRole("button", { name, exact: false });
      if ((await button.count()) > 0) {
        await clickAndSettle(page, button.first());
        return name;
      }
    }
  }
  throw new Error(`form_button_not_found:${formAction}:${preferredNames.join(",")}`);
}

async function waitForPolicyConfirmation(page, timeout = 90000) {
  try {
    await page.locator('form[action="/chat/policy-confirmation"]').first().waitFor({ timeout });
    await waitForVisibleText(page, "아침 08:00", 5000);
    return true;
  } catch (_) {
    await assertNoAgentError(page);
    return false;
  }
}

async function writeReport(summary) {
  const lines = [];
  lines.push("# Real LLM Demo Video Playwright Report");
  lines.push("");
  lines.push(`- Run ID: ${summary.runId}`);
  lines.push(`- Target URL: ${summary.baseUrl}`);
  lines.push(`- Output directory: ${summary.outputDir}`);
  if (summary.video) {
    lines.push(`- Video: ${summary.video}`);
  }
  lines.push("- Scope: 실제 UI 조작 + 실제 agent_app/PHR/system_app + Bedrock LLM tool 판단 경로");
  lines.push("");
  lines.push("## Demo Flow");
  lines.push("");
  for (const [index, step] of summary.steps.entries()) {
    lines.push(`${index + 1}. ${step.title}${step.detail ? ` - ${step.detail}` : ""}`);
  }
  lines.push("");
  lines.push("## Evidence");
  lines.push("");
  for (const item of summary.evidence) {
    lines.push(`- ${item}`);
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
  page.on("dialog", (dialog) => dialog.accept());
  let videoPath = "";
  const evidence = [];

  try {
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await showStep(page, "1. 시연 시작: 초기 상태로 리셋", "기존 데이터가 데모에 섞이지 않도록 전체 리셋부터 진행합니다.");
    await clickAndSettle(page, page.getByRole("button", { name: "전체 리셋" }));
    await showStep(page, "2. 복약 계획 생성", "화면에서 당뇨약을 선택하고 기본 아침/점심/야간 복약 일정을 등록합니다.");
    await page.locator('input[name="medication_choice"][value="당뇨약"]').check({ force: true });
    await page.locator('textarea[name="instructions"]').fill("실제 시연용: 부작용/알림 안전 확인 플로우 검증");
    await clickAndSettle(page, page.getByRole("button", { name: "복약 일정 추가" }));
    await waitForVisibleText(page, "당뇨약", 30000);
    evidence.push("복약 계획이 UI에서 등록됨");

    await showStep(page, "3. PHR 등록", "부작용 조회가 복용약 주의사항을 볼 수 있도록 PHR key를 발급합니다.");
    await clickAndSettle(page, page.getByRole("button", { name: "설정 완료 및 PHR 등록" }));
    await waitForVisibleText(page, "PHR key 발급 완료", 60000);
    evidence.push("PHR key 발급 완료 상태 확인");

    await showStep(page, "4. 시뮬레이션 시간 진행", "아침 약을 복용하지 않은 채 3시간을 진행해 미복용 AI 대화가 만들어지는지 봅니다.");
    await clickAndSettle(page, page.getByRole("button", { name: "3시간 진행" }));
    await waitForMissedDoseConversationAlert(page, 180000);
    evidence.push("미복용 conversation alert 생성 확인");

    const openChatButtons = page.locator("[data-open-chat]");
    if ((await openChatButtons.count()) > 0) {
      await showStep(page, "5. 알림에서 채팅 열기", "미복용 알림의 채팅 열기 버튼으로 에이전트 대화 화면에 진입합니다.");
      await clickAndSettle(page, openChatButtons.first());
      await page.locator("#chat-page.is-active").waitFor({ timeout: 10000 }).catch(async () => {
        await clickChatTab(page);
      });
    } else {
      await showStep(page, "5. 채팅 화면으로 이동", "미복용 대화가 쌓인 채팅 화면을 직접 엽니다.");
      await clickChatTab(page);
    }

    await showStep(page, "6. 부작용 질문", "환자가 메스꺼움을 말하면 Agent가 PHR 조회와 PRO-CTCAE tool 경로를 판단해야 합니다.");
    await sendChat(page, "속이 메스꺼운데 당뇨약 부작용일까? 필요한 문항이 있으면 진행해줘.");
    await waitForAeOptions(page);
    evidence.push("실제 LLM 경로에서 PRO-CTCAE 문항 표시 확인");

    await showStep(page, "7. PRO-CTCAE 응답 기록", "빈도와 정도 응답이 모두 기록되면 deterministic 안전 안내가 이어져야 합니다.");
    const firstAnswer = await clickAeAnswer(page, 0, ["자주 있다", "가끔 있다", "거의 항상 있다"]);
    const secondAnswer = await clickAeAnswer(page, 1, ["보통이다", "심하다", "약간 있다"]);
    await waitForVisibleText(page, "부작용에 대해 기록했습니다", 30000);
    evidence.push(`PRO-CTCAE 응답 기록 완료: ${firstAnswer}, ${secondAnswer}`);

    await showStep(page, "8. 부작용 이후 알림 안전 선택", "환자가 알림 모두 끄기를 선택하면 복약/미복용/daily pattern 알림이 함께 중지되어야 합니다.");
    await clickLatestFormButton(page, "/chat/side-effect-reminder-safety", ["알림 모두 끄기"]);
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    await showStep(page, "8-1. 알림 중지 상태 반영 확인", "전체 화면을 새로 읽어 정책 패널과 시뮬레이션 제어 패널이 같은 상태를 보는지 확인합니다.");
    await clickHomeTab(page);
    await waitForVisibleText(page, "알림 전체 켜기", 30000);
    const manualButtonDisabled = await page.locator('form[action="/analysis/run-today"] button').isDisabled();
    if (!manualButtonDisabled) {
      throw new Error("manual_analysis_button_not_disabled_after_suppression");
    }
    evidence.push("알림 전체 꺼짐 상태와 수동 패턴 분석 비활성화 확인");

    await showStep(page, "9. 프론트 UI에서 알림 다시 켜기", "사용자가 정책 패널에서 알림 전체 상태를 즉시 되돌릴 수 있어야 합니다.");
    await clickAndSettle(page, page.getByRole("button", { name: "알림 전체 켜기" }));
    await waitForVisibleText(page, "알림 전체 끄기", 30000);
    evidence.push("프론트 UI 알림 전체 켜기 동작 확인");

    await showStep(page, "10. 채팅으로 정책 변경 요청", "알림 강화 요청은 바로 적용되지 않고 confirmation 카드로 멈춰야 합니다.");
    await clickChatTab(page);
    await sendChat(page, "아침 08:00 당뇨약 복약 알림을 추가 2회, 10분 간격으로 늘려줘. 정책 후보를 만들고 확인을 받아줘.");
    let policyConfirmationReady = await waitForPolicyConfirmation(page);
    if (!policyConfirmationReady) {
      evidence.push("1차 정책 요청은 안내 답변만 생성되어 tool-explicit follow-up으로 재시도함");
      await showStep(page, "10-1. 정책 후보 생성 재요청", "LLM이 설명만 한 경우 실제 정책 후보 확인 카드 생성을 더 명확히 요청합니다.");
      await sendChat(page, "방금 요청은 실제 알림 정책 후보 확인 카드로 만들어줘. apply_notification_policy 도구를 사용해서 아침 08:00 추가 2회, 10분 간격 후보를 생성해줘.");
      policyConfirmationReady = await waitForPolicyConfirmation(page, 180000);
    }
    if (!policyConfirmationReady) {
      throw new Error("policy_confirmation_not_created_after_retry");
    }
    evidence.push("정책 변경 confirmation 카드 생성 확인");

    await showStep(page, "11. 사용자가 정책 변경을 확인", "늘리기를 선택한 뒤에만 활성 알림 정책에 맞춤 정책이 반영됩니다.");
    await clickLatestFormButton(page, "/chat/policy-confirmation", ["늘리기", "적용하기"]);
    await clickHomeTab(page);
    await waitForBodyText(page, "2회 추가", 30000);
    await waitForBodyText(page, "10분 간격", 30000);
    evidence.push("사용자 확인 후 알림 정책 적용 확인");

    await showStep(page, "12. 멀티턴 회상 확인", "에이전트가 방금 대화의 증상 맥락을 일반 대화로 회상하는지 확인합니다.");
    await clickChatTab(page);
    const beforeRecallCount = await chatMessageCount(page);
    await sendChat(page, "방금 내가 어떤 증상을 말했더라?");
    await page.waitForFunction(
      (minCount) => {
        const text = document.body.innerText;
        const region = document.getElementById("chat-log-region");
        const count = Number((region && region.getAttribute("data-chat-message-count")) || "0");
        return count >= minCount && (text.includes("메스꺼") || text.includes("속이"));
      },
      beforeRecallCount + 2,
      { timeout: 180000 },
    );
    await assertNoAgentError(page);
    const finalText = await bodyText(page);
    if (finalText.includes("요청을 확인했습니다.")) {
      throw new Error("generic_fallback_reply_visible_in_demo");
    }
    evidence.push("멀티턴 회상 응답에서 메스꺼움 맥락 확인");

    await showStep(page, "시연 완료", "미복용 대화, 부작용 안전 확인, 알림 전체 토글, 정책 confirmation, 멀티턴 회상을 한 흐름으로 검증했습니다.");
    await page.screenshot({ path: path.join(OUT_DIR, "final-demo-state.png"), fullPage: true });
  } catch (error) {
    await page.screenshot({ path: path.join(OUT_DIR, "failed-demo-state.png"), fullPage: true }).catch(() => {});
    throw error;
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
    video: videoPath ? path.relative(process.cwd(), videoPath) : "",
    status: "passed",
    steps,
    evidence,
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  await writeReport(summary);
  console.log(JSON.stringify(summary, null, 2));
}

main().catch((error) => {
  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    outputDir: OUT_DIR,
    status: "failed",
    error: error.message || String(error),
    steps,
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  console.error(error);
  process.exit(1);
});

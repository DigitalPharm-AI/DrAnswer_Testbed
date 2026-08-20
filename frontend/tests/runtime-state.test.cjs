const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const test = require("node:test");
const { chromium } = require(
  path.resolve(__dirname, "../../node_modules/playwright-core"),
);

const buildRoot = path.resolve(
  __dirname,
  "../../system_app/static/react",
);

const dashboard = {
  features: {
    medication_side_effect_enabled: true,
  },
  clock: {
    current_time: "2026-07-26T09:00:00+09:00",
    is_running: false,
    speed_multiplier: 60,
  },
  simulation_ready: true,
  active_scenario: null,
  medications: [],
  nutrition: {
    profile: {},
    summary: {
      date: "2026-07-26",
      total_meals: 0,
    },
    preferences: {},
    metrics: [],
    meals: [],
    scenarios: [],
  },
  policies: {
    medication_schedule_alert: false,
    missed_dose_conversation: false,
  },
  notifications: [],
};

const medicationStatusDashboard = {
  ...dashboard,
  active_scenario: {
    scenario_id: "status-display-scenario",
    name: "상태 표시 검증",
    schedule_date: "2026-07-26",
  },
  medications: [
    {
      dose_event_id: "dose-taken",
      medication_name: "메트포르민 500mg",
      treatment_area: "당뇨약",
      slot_label: "아침",
      scheduled_for: "2026-07-26T08:00:00+09:00",
      status: "taken",
      taken_at: "2026-07-26T08:03:00+09:00",
    },
    {
      dose_event_id: "dose-missed",
      medication_name: "암로디핀 5mg",
      treatment_area: "고혈압약",
      slot_label: "점심",
      scheduled_for: "2026-07-26T12:00:00+09:00",
      status: "missed",
      taken_at: null,
    },
    {
      dose_event_id: "dose-scheduled",
      medication_name: "레트로졸 2.5mg",
      treatment_area: "유방암 치료약",
      slot_label: "저녁",
      scheduled_for: "2026-07-26T18:00:00+09:00",
      status: "scheduled",
      taken_at: null,
    },
  ],
};

const scenarios = { scenarios: [] };
const chatHistory = { days: [], next_before_date: null };
const assistantHistory = {
  days: [
    {
      date: "2026-07-26",
      messages: [
        {
          message_id: "assistant-history-1",
          role: "assistant",
          message_type: "text",
          message: null,
          content: {
            message_title: "복약 안내",
            text: "서버에서 받은 실제 답변입니다.",
            tables: null,
            selections: null,
            inputs: null,
          },
          created_at: "2026-07-26T09:00:00+09:00",
          processing_status: "completed",
          opinion_submitted: false,
          reaction: null,
        },
      ],
    },
  ],
  next_before_date: null,
};
const defaultPortionInputHistory = {
  days: [
    {
      date: "2026-07-26",
      messages: [
        {
          message_id: "assistant-default-portion-input",
          sort_sequence: 401,
          role: "assistant",
          source_message_id: null,
          response_message_id: null,
          message_type: "input_box",
          message: null,
          content: {
            message_title: "섭취량 입력",
            text: "선택한 음식별 실제 섭취량을 입력해 주세요.",
            tables: null,
            selections: null,
            inputs: [
              {
                type: "number",
                label: "1. 토스트_마늘토스트 섭취량",
                value: 500,
                options: {
                  unit: "g",
                  lower: 1,
                  upper: 5000,
                  selections: null,
                },
              },
              {
                type: "number",
                label: "2. 물_생수 섭취량",
                value: 1000,
                options: {
                  unit: "mL",
                  lower: 1,
                  upper: 5000,
                  selections: null,
                },
              },
            ],
          },
          created_at: "2026-07-26T09:30:00+09:00",
          processing_status: "pending",
          opinion_submitted: false,
          opinion_submitted_at: null,
          reaction: null,
        },
      ],
    },
  ],
  next_before_date: null,
};
const proactiveNotification = {
  id: "notif-proactive-chat-1",
  notification_type: "conversation_alert",
  title: "AI가 대화를 요청합니다.",
  body: "복약을 놓친 이유를 알려주세요.",
  visible_at: "2026-07-26T09:00:00+09:00",
  visible_at_label: "07-26 09:00",
  acknowledged: false,
  metadata: { severity: "reminder" },
  interaction: {
    kind: "open_chat",
    state: "ready",
    message_id: "assistant-proactive-1",
  },
  dose_status: "missed",
  related_dose_event_id: "dose-proactive-1",
};
const proactiveChatHistory = {
  days: [
    {
      date: "2026-07-26",
      messages: [
        {
          message_id: "assistant-proactive-1",
          sort_sequence: 301,
          role: "assistant",
          source_message_id: null,
          response_message_id: null,
          message_type: "text",
          message: null,
          content: {
            message_title: null,
            text: "복약을 놓친 이유를 알려주세요.",
            tables: null,
            selections: null,
            inputs: null,
          },
          created_at: "2026-07-26T09:00:00+09:00",
          processing_status: "completed",
          opinion_submitted: false,
          reaction: null,
        },
      ],
    },
  ],
  next_before_date: null,
};
const markdownAssistantHistory = {
  days: [
    {
      date: "2026-07-26",
      messages: [
        {
          message_id: "assistant-markdown-1",
          role: "assistant",
          message_type: "text",
          message: null,
          content: {
            message_title: null,
            text:
              "메스꺼움이 현재 복용 중인 약들과 관련이 있을 수 있습니다.\n\n현재 다음 3가지 약에서 메스꺼움이 부작용으로 보고되어 있습니다:\n\n- **메트포르민 500mg** (당뇨약)\n\n- **수니티닙 50mg** (신장암 치료약)\n\n- **레트로졸 2.5mg** (유방암 치료약)\n\n**어느 약을 복용한 후에 메스꺼움이 시작되었나요?** 또는 **언제부터 속이 메스꺼워졌나요?**\n\n1. 아침 복용\n2. 저녁 복용\n\n<script>window.__markdownProbe = true</script>\n\n| Time | Medication | Status |\n|---|---|---|\n| 08:00 | Metformin | Taken |\n| 18:00 | Letrozole | Scheduled |",
            tables: null,
            selections: null,
            inputs: null,
          },
          created_at: "2026-07-26T09:00:00+09:00",
          processing_status: "completed",
          opinion_submitted: false,
          reaction: null,
        },
      ],
    },
  ],
  next_before_date: null,
};
const explicitlyLinkedStructuredHistory = {
  days: [
    {
      date: "2026-07-26",
      messages: [
        {
          message_id: "assistant-linked-selection",
          role: "assistant",
          source_message_id: null,
          response_message_id: "user-linked-correct",
          message_type: "selection_box",
          message: null,
          content: {
            message_title: "기록 확인",
            text: "저장할 내용을 확인해 주세요.",
            tables: null,
            selections: ["정확한 선택", "취소"],
            inputs: null,
          },
          created_at: "2026-07-26T09:00:00+09:00",
          processing_status: "answered",
          opinion_submitted: false,
          reaction: null,
        },
        {
          message_id: "user-linked-distractor",
          role: "user",
          source_message_id: "assistant-unrelated",
          response_message_id: null,
          message_type: "selection_box",
          message: "잘못된 선택",
          content: null,
          created_at: "2026-07-26T09:00:10+09:00",
          processing_status: "completed",
          opinion_submitted: false,
          reaction: null,
        },
        {
          message_id: "user-linked-correct",
          role: "user",
          source_message_id: "assistant-linked-selection",
          response_message_id: null,
          message_type: "selection_box",
          message: "정확한 선택",
          content: null,
          created_at: "2026-07-26T09:00:20+09:00",
          processing_status: "completed",
          opinion_submitted: false,
          reaction: null,
        },
      ],
    },
  ],
  next_before_date: null,
};
const crossPageLinkedCurrentHistory = {
  days: [
    {
      date: "2026-07-26",
      messages: [
        {
          message_id: "user-cross-page-distractor",
          role: "user",
          source_message_id: "assistant-cross-page-unrelated",
          response_message_id: null,
          message_type: "selection_box",
          message: "다른 응답",
          content: null,
          created_at: "2026-07-26T08:59:00+09:00",
          processing_status: "completed",
          opinion_submitted: false,
          reaction: null,
        },
        {
          message_id: "user-cross-page-correct",
          role: "user",
          source_message_id: "assistant-cross-page-selection",
          response_message_id: null,
          message_type: "selection_box",
          message: "정확한 과거 응답",
          content: null,
          created_at: "2026-07-26T09:00:00+09:00",
          processing_status: "completed",
          opinion_submitted: false,
          reaction: null,
        },
      ],
    },
  ],
  next_before_date: "2026-07-26",
};
const crossPageLinkedPreviousHistory = {
  days: [
    {
      date: "2026-07-25",
      messages: [
        {
          message_id: "assistant-cross-page-selection",
          role: "assistant",
          source_message_id: null,
          response_message_id: "user-cross-page-correct",
          message_type: "selection_box",
          message: null,
          content: {
            message_title: "과거 기록 확인",
            text: "이전 날짜에 요청한 선택입니다.",
            tables: null,
            selections: ["정확한 과거 응답", "취소"],
            inputs: null,
          },
          created_at: "2026-07-25T23:59:00+09:00",
          processing_status: "answered",
          opinion_submitted: false,
          reaction: null,
        },
        {
          // A repeated Backend row must not create a duplicate React message.
          message_id: "assistant-cross-page-selection",
          role: "assistant",
          source_message_id: null,
          response_message_id: "user-cross-page-correct",
          message_type: "selection_box",
          message: null,
          content: {
            message_title: "과거 기록 확인",
            text: "이전 날짜에 요청한 선택입니다.",
            tables: null,
            selections: ["정확한 과거 응답", "취소"],
            inputs: null,
          },
          created_at: "2026-07-25T23:59:00+09:00",
          processing_status: "answered",
          opinion_submitted: false,
          reaction: null,
        },
      ],
    },
  ],
  next_before_date: null,
};
const readyStatus = {
  backend_server: {
    status: "ready",
    checked_at: "2026-07-26T00:00:00Z",
    evidence: ["Backend UI API 응답 성공"],
  },
  ai_server: {
    status: "ready",
    checked_at: "2026-07-26T00:00:00Z",
    evidence: ["Bedrock 최소 호출 성공"],
  },
};

function success(data) {
  return { success: true, data, error: null };
}

function failure(message, code = "TEST_FAILURE", retryable = true) {
  return {
    success: false,
    data: null,
    error: {
      code,
      message,
      retryable,
      details: null,
    },
  };
}

function chatSuccess(requestId) {
  return success({
    request_id: requestId,
    user_message_id: "user-public-1",
    user_sort_sequence: 101,
    assistant_message_id: "assistant-public-1",
    assistant_sort_sequence: 102,
    message_type: "text",
    message: {
      message_title: null,
      text: "서버에서 생성한 실제 답변입니다.",
      tables: null,
      selections: null,
      inputs: null,
    },
    message_at: "2026-07-26T09:01:00+09:00",
    display_message_at: "2026-07-26T09:01:00+09:00",
  });
}

async function fulfillChatStream(route, envelope) {
  assert.equal(envelope.success, true);
  const data = envelope.data;
  const frames = [
    `event: start\ndata: ${JSON.stringify({
      request_id: data.request_id,
      status: "processing",
    })}\n\n`,
  ];
  if (data.message_type === "text" && data.message.text) {
    frames.push(
      `event: text_delta\ndata: ${JSON.stringify({
        request_id: data.request_id,
        sequence: 0,
        text: data.message.text,
      })}\n\n`,
    );
  }
  frames.push(`event: completed\ndata: ${JSON.stringify(data)}\n\n`);
  await route.fulfill({
    status: 200,
    contentType: "text/event-stream; charset=utf-8",
    headers: {
      "Cache-Control": "no-cache",
    },
    body: frames.join(""),
  });
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function sseFrame(eventName, data) {
  return `event: ${eventName}\ndata: ${JSON.stringify(data)}\n\n`;
}

async function readJsonRequest(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function createControlledChatStream({
  firstDelta = "서버에서 생성한 ",
  secondDelta = "실제 답변입니다.",
  completedText = "서버에서 생성한 실제 답변입니다.",
} = {}) {
  const started = deferred();
  const allowFirstDelta = deferred();
  const firstDeltaWritten = deferred();
  const allowSecondDelta = deferred();
  const secondDeltaWritten = deferred();
  const allowCompletion = deferred();

  async function handle(request, response) {
    const payload = await readJsonRequest(request);
    const completed = chatSuccess(payload.request_id).data;
    completed.message.text = completedText;

    response.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
    response.flushHeaders?.();
    response.write(
      sseFrame("start", {
        request_id: payload.request_id,
        status: "processing",
      }),
    );
    started.resolve(payload);

    await allowFirstDelta.promise;
    if (response.destroyed) {
      return;
    }
    response.write(
      sseFrame("text_delta", {
        request_id: payload.request_id,
        sequence: 0,
        text: firstDelta,
      }),
    );
    firstDeltaWritten.resolve();

    await allowSecondDelta.promise;
    if (response.destroyed) {
      return;
    }
    response.write(
      sseFrame("text_delta", {
        request_id: payload.request_id,
        sequence: 1,
        text: secondDelta,
      }),
    );
    secondDeltaWritten.resolve();

    await allowCompletion.promise;
    if (response.destroyed) {
      return;
    }
    response.end(sseFrame("completed", completed));
  }

  return {
    handle,
    started: started.promise,
    firstDeltaWritten: firstDeltaWritten.promise,
    secondDeltaWritten: secondDeltaWritten.promise,
    releaseFirstDelta: () => allowFirstDelta.resolve(),
    releaseSecondDelta: () => allowSecondDelta.resolve(),
    finish: () => allowCompletion.resolve(),
    releaseCompletion() {
      allowSecondDelta.resolve();
      allowCompletion.resolve();
    },
    releaseAll() {
      allowFirstDelta.resolve();
      allowSecondDelta.resolve();
      allowCompletion.resolve();
    },
  };
}

function symptomSelectionSuccess(requestId) {
  return success({
    request_id: requestId,
    user_message_id: "user-symptom-1",
    user_sort_sequence: 201,
    assistant_message_id: "assistant-symptom-1",
    assistant_sort_sequence: 202,
    message_type: "selection_box",
    message: {
      message_title: "증상 평가",
      text:
        "메스꺼움 증상이 약물과 관련이 있을 수 있습니다. 아래의 질문에 답변해 주시면 증상을 더 정확하게 평가할 수 있습니다.\n\n지난 7일 동안 메스꺼움의 정도는 어땠나요?",
      tables: null,
      selections: ["없음", "경미함", "중간 정도", "심함"],
      inputs: null,
    },
    message_at: "2026-07-26T09:01:00+09:00",
    display_message_at: "2026-07-26T09:01:00+09:00",
  });
}

function responseFor(pathname) {
  if (pathname.endsWith("/dashboard")) {
    return success(dashboard);
  }
  if (pathname.endsWith("/medication-scenarios")) {
    return success(scenarios);
  }
  if (pathname.endsWith("/chat/history")) {
    return success(chatHistory);
  }
  if (pathname.endsWith("/status")) {
    return success(readyStatus);
  }
  throw new Error(`Unexpected API request: ${pathname}`);
}

function chromeExecutable() {
  const candidates = [
    process.env.CHROME_PATH,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  const executable = candidates.find((candidate) => fs.existsSync(candidate));
  if (!executable) {
    throw new Error(`Chrome executable not found: ${candidates.join(", ")}`);
  }
  return executable;
}

function contentType(filePath) {
  switch (path.extname(filePath)) {
    case ".css":
      return "text/css; charset=utf-8";
    case ".html":
      return "text/html; charset=utf-8";
    case ".js":
      return "text/javascript; charset=utf-8";
    default:
      return "application/octet-stream";
  }
}

function startStaticServer() {
  let chatStreamHandler = null;
  assert.ok(
    fs.existsSync(path.join(buildRoot, "index.html")),
    "React build is missing. Run `npm run build` before this test.",
  );

  const server = http.createServer((request, response) => {
    const requestUrl = new URL(request.url, "http://127.0.0.1");
    if (
      request.method === "POST" &&
      requestUrl.pathname === "/api/ui/v1/chat/stream" &&
      chatStreamHandler
    ) {
      Promise.resolve(chatStreamHandler(request, response)).catch((error) => {
        if (!response.headersSent) {
          response.writeHead(500).end();
        } else {
          response.destroy(error);
        }
      });
      return;
    }
    let filePath;
    if (
      requestUrl.pathname === "/"
    ) {
      filePath = path.join(buildRoot, "index.html");
    } else if (requestUrl.pathname.startsWith("/static/react/")) {
      const relativePath = requestUrl.pathname.slice("/static/react/".length);
      filePath = path.resolve(buildRoot, relativePath);
      if (
        filePath !== buildRoot &&
        !filePath.startsWith(`${buildRoot}${path.sep}`)
      ) {
        response.writeHead(403).end();
        return;
      }
    } else {
      response.writeHead(404).end();
      return;
    }

    fs.readFile(filePath, (error, data) => {
      if (error) {
        response.writeHead(error.code === "ENOENT" ? 404 : 500).end();
        return;
      }
      response.writeHead(200, { "Content-Type": contentType(filePath) });
      response.end(data);
    });
  });

  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve({
        server,
        baseUrl: `http://127.0.0.1:${address.port}`,
        setChatStreamHandler(handler) {
          chatStreamHandler = handler;
        },
      });
    });
  });
}

async function closeServer(server) {
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function fulfillJson(route, body, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

async function withPage(browser, baseUrl, handler, assertions) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  await page.route("**/api/ui/v1/**", handler);
  try {
    await page.goto(baseUrl, {
      waitUntil: "domcontentloaded",
      timeout: 15_000,
    });
    await assertions(page);
  } finally {
    await context.close();
  }
}

test(
  "React runtime uses only live API data and exposes failures",
  { timeout: 60_000 },
  async (suite) => {
    const { server, baseUrl, setChatStreamHandler } =
      await startStaticServer();
    const browser = await chromium.launch({
      executablePath: chromeExecutable(),
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    });

    try {
      await suite.test("shows loading until all required APIs succeed", async () => {
        let releaseRequests;
        const requestGate = new Promise((resolve) => {
          releaseRequests = resolve;
        });

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            await requestGate;
            await fulfillJson(
              route,
              responseFor(new URL(route.request().url()).pathname),
            );
          },
          async (page) => {
            await page
              .getByText("Backend 데이터를 불러오는 중입니다.")
              .waitFor();
            const loadingText = await page.locator("body").innerText();
            assert.doesNotMatch(loadingText, /메트포르민|tb-combined-v1/);

            releaseRequests();
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
            assert.equal(
              await page.getByText("아직 대화가 없습니다.").isVisible(),
              false,
            );
          },
        );
      });

      await suite.test(
        "keeps Chat empty when a pre-reset history request finishes late",
        async () => {
          const staleHistoryStarted = deferred();
          const releaseStaleHistory = deferred();
          const staleHistoryFinished = deferred();
          let resetApplied = false;

          const currentHistory = {
            ...assistantHistory,
            next_before_date: "2026-07-25",
          };
          const stalePreviousHistory = {
            days: [
              {
                date: "2026-07-25",
                messages: [
                  {
                    ...assistantHistory.days[0].messages[0],
                    message_id: "assistant-stale-before-reset",
                    content: {
                      ...assistantHistory.days[0].messages[0].content,
                      text: "초기화 전에 요청한 이전 대화입니다.",
                    },
                    created_at: "2026-07-25T09:00:00+09:00",
                  },
                ],
              },
            ],
            next_before_date: null,
          };

          await withPage(
            browser,
            baseUrl,
            async (route) => {
              const requestUrl = new URL(route.request().url());
              const pathname = requestUrl.pathname;
              if (pathname.endsWith("/testbed/reset")) {
                resetApplied = true;
                await fulfillJson(
                  route,
                  success({
                    request_id: JSON.parse(route.request().postData()).request_id,
                    reset_applied: true,
                    reset_at: "2026-07-26T00:01:00Z",
                  }),
                );
                return;
              }
              if (pathname.endsWith("/chat/history")) {
                if (requestUrl.searchParams.has("before_date")) {
                  staleHistoryStarted.resolve();
                  await releaseStaleHistory.promise;
                  await fulfillJson(route, success(stalePreviousHistory));
                  staleHistoryFinished.resolve();
                  return;
                }
                await fulfillJson(
                  route,
                  success(resetApplied ? chatHistory : currentHistory),
                );
                return;
              }
              await fulfillJson(route, responseFor(pathname));
            },
            async (page) => {
              await page
                .getByText("오늘 등록된 복약 일정이 없습니다.")
                .waitFor();
              await page.getByRole("tab", { name: "Chat" }).click();
              await page
                .locator('[data-message-id="assistant-history-1"]')
                .waitFor();

              await page.locator("#chat-log").dispatchEvent("wheel", {
                deltaY: -120,
              });
              await staleHistoryStarted.promise;

              await page.getByRole("tab", { name: "Home" }).click();
              page.once("dialog", (dialog) => dialog.accept());
              await page.getByRole("button", { name: "전체 리셋" }).click();
              await page
                .getByText("테스트 환경을 초기화했습니다.", { exact: true })
                .waitFor();

              releaseStaleHistory.resolve();
              await staleHistoryFinished.promise;
              await page.getByRole("tab", { name: "Chat" }).click();
              await page
                .getByText("아직 대화가 없습니다.", { exact: true })
                .waitFor();
              await page.waitForTimeout(100);

              assert.equal(
                await page
                  .locator('[data-message-id="assistant-history-1"]')
                  .count(),
                0,
              );
              assert.equal(
                await page
                  .locator(
                    '[data-message-id="assistant-stale-before-reset"]',
                  )
                  .count(),
                0,
              );
            },
          );
        },
      );

      await suite.test(
        "syncs a ready proactive message into Chat without opening its notification",
        async () => {
          let proactiveReady = false;
          let chatHistoryRequestCount = 0;

          await withPage(
            browser,
            baseUrl,
            async (route) => {
              const pathname = new URL(route.request().url()).pathname;
              if (pathname.endsWith("/dashboard")) {
                await fulfillJson(
                  route,
                  success(
                    proactiveReady
                      ? {
                          ...dashboard,
                          notifications: [proactiveNotification],
                        }
                      : dashboard,
                  ),
                );
                return;
              }
              if (pathname.endsWith("/chat/history")) {
                chatHistoryRequestCount += 1;
                await fulfillJson(
                  route,
                  success(
                    proactiveReady ? proactiveChatHistory : chatHistory,
                  ),
                );
                return;
              }
              await fulfillJson(route, responseFor(pathname));
            },
            async (page) => {
              await page
                .getByText("오늘 등록된 복약 일정이 없습니다.")
                .waitFor();
              assert.equal(chatHistoryRequestCount, 1);

              proactiveReady = true;
              await page
                .getByRole("button", { name: "대화 확인" })
                .waitFor({ timeout: 8_000 });

              // Deliberately do not click the notification action. The
              // dashboard reconciliation must already have synchronized Chat.
              await page.getByRole("tab", { name: "Chat" }).click();
              await page
                .getByRole("log", { name: "대화 내용" })
                .getByText("복약을 놓친 이유를 알려주세요.")
                .waitFor();
              assert.ok(chatHistoryRequestCount >= 2);
            },
          );
        },
      );

      await suite.test(
        "prefills portion inputs and adjusts by their default magnitude",
        async () => {
          await withPage(
            browser,
            baseUrl,
            async (route) => {
              const pathname = new URL(route.request().url()).pathname;
              if (pathname.endsWith("/chat/history")) {
                await fulfillJson(route, success(defaultPortionInputHistory));
                return;
              }
              await fulfillJson(route, responseFor(pathname));
            },
            async (page) => {
              await page
                .getByText("오늘 등록된 복약 일정이 없습니다.")
                .waitFor();
              await page.getByRole("tab", { name: "Chat" }).click();

              const toast = page.getByLabel(
                "1. 토스트_마늘토스트 섭취량",
                { exact: true },
              );
              const water = page.getByLabel("2. 물_생수 섭취량", {
                exact: true,
              });
              await toast.waitFor();

              assert.equal(await toast.inputValue(), "500");
              assert.equal(await toast.getAttribute("step"), "10");
              assert.equal(await water.inputValue(), "1000");
              assert.equal(await water.getAttribute("step"), "100");
              await page
                .getByText("기준 제공량 500g · 10g 단위 조정", {
                  exact: true,
                })
                .waitFor();
              await page
                .getByText("기준 제공량 1,000mL · 100mL 단위 조정", {
                  exact: true,
                })
                .waitFor();

              await toast.press("ArrowUp");
              await water.press("ArrowDown");
              assert.equal(await toast.inputValue(), "510");
              assert.equal(await water.inputValue(), "900");
            },
          );
        },
      );

      await suite.test("shows a blocking error and supports retry", async () => {
        let backendAvailable = false;

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            if (!backendAvailable) {
              await fulfillJson(
                route,
                failure("테스트 Backend 연결 실패", "NETWORK_ERROR"),
                503,
              );
              return;
            }
            await fulfillJson(
              route,
              responseFor(new URL(route.request().url()).pathname),
            );
          },
          async (page) => {
            await page
              .getByText("Backend 연결을 확인해 주세요.")
              .waitFor();
            assert.match(
              await page.locator(".runtime-state-panel").innerText(),
              /테스트 Backend 연결 실패/,
            );

            backendAvailable = true;
            await page.getByRole("button", { name: "다시 연결" }).click();
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
          },
        );
      });

      await suite.test("rejects a malformed success envelope", async () => {
        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            await fulfillJson(
              route,
              pathname.endsWith("/dashboard")
                ? { success: true, data: dashboard, error: {} }
                : responseFor(pathname),
            );
          },
          async (page) => {
            await page
              .getByText("Backend 연결을 확인해 주세요.")
              .waitFor();
            assert.match(
              await page.locator(".runtime-state-panel").innerText(),
              /서버 응답을 확인할 수 없습니다/,
            );
            assert.equal(
              await page
                .getByText("오늘 등록된 복약 일정이 없습니다.")
                .count(),
              0,
            );
          },
        );
      });

      await suite.test("renders failed and timeout statuses as abnormal", async () => {
        const abnormalStatus = {
          backend_server: {
            status: "failed",
            checked_at: "2026-07-26T00:00:00Z",
            evidence: ["Backend 자체 점검 실패"],
          },
          ai_server: {
            status: "timeout",
            checked_at: "2026-07-26T00:00:00Z",
            evidence: ["Bedrock 호출 시간 초과"],
          },
        };

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            await fulfillJson(
              route,
              pathname.endsWith("/status")
                ? success(abnormalStatus)
                : responseFor(pathname),
            );
          },
          async (page) => {
            const backend = page.locator(".service-state.is-failed");
            const aiServer = page.locator(".service-state.is-timeout");
            await backend.waitFor();
            await aiServer.waitFor();
            assert.equal((await backend.innerText()).trim(), "Backend 오류");
            assert.equal(
              (await aiServer.innerText()).trim(),
              "AI Server 시간 초과",
            );
            assert.match(
              await backend.getAttribute("title"),
              /Backend 자체 점검 실패/,
            );
            assert.match(
              await aiServer.getAttribute("title"),
              /Bedrock 호출 시간 초과/,
            );
          },
        );
      });

      await suite.test("renders not-ready and degraded statuses as abnormal", async () => {
        const abnormalStatus = {
          backend_server: {
            status: "degraded",
            checked_at: "2026-07-26T00:00:00Z",
            evidence: ["일부 구성요소 장애"],
          },
          ai_server: {
            status: "not_ready",
            checked_at: "2026-07-26T00:00:00Z",
            evidence: ["생성 공급자 준비 안 됨"],
          },
        };

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            await fulfillJson(
              route,
              pathname.endsWith("/status")
                ? success(abnormalStatus)
                : responseFor(pathname),
            );
          },
          async (page) => {
            const backend = page.locator(
              '.service-state.is-degraded[data-status="degraded"]',
            );
            const aiServer = page.locator(
              '.service-state.is-not_ready[data-status="not_ready"]',
            );
            await backend.waitFor();
            await aiServer.waitFor();
            assert.equal(
              (await backend.innerText()).trim(),
              "Backend 일부 장애",
            );
            assert.equal(
              (await aiServer.innerText()).trim(),
              "AI Server 점검 필요",
            );
            assert.equal(
              await backend.evaluate(
                (element) => getComputedStyle(element).backgroundColor,
              ),
              "rgb(251, 233, 234)",
            );
            assert.equal(
              await aiServer.evaluate(
                (element) => getComputedStyle(element).backgroundColor,
              ),
              "rgb(251, 233, 234)",
            );
            for (const indicator of [backend, aiServer]) {
              assert.equal(
                await indicator
                  .locator(":scope > span")
                  .evaluate(
                    (element) =>
                      getComputedStyle(element).backgroundColor,
                  ),
                "rgb(201, 79, 88)",
              );
            }
            assert.equal(
              await page.locator(".service-state.is-ready").count(),
              0,
            );
          },
        );
      });

      await suite.test("distinguishes missed medication from taken and scheduled doses", async () => {
        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            await fulfillJson(
              route,
              pathname.endsWith("/dashboard")
                ? success(medicationStatusDashboard)
                : responseFor(pathname),
            );
          },
          async (page) => {
            const missedCard = page.locator(
              '.medication-item.is-missed:has-text("암로디핀 5mg")',
            );
            const takenCard = page.locator(
              '.medication-item.is-complete:has-text("메트포르민 500mg")',
            );
            const scheduledCard = page.locator(
              '.medication-item:not(.is-complete):not(.is-missed):has-text("레트로졸 2.5mg")',
            );
            await missedCard.waitFor();
            assert.match(await missedCard.innerText(), /미복용/);
            assert.match(await takenCard.innerText(), /복용/);
            assert.match(await scheduledCard.innerText(), /예정/);
            assert.equal(
              await missedCard.evaluate(
                (element) => getComputedStyle(element).backgroundColor,
              ),
              "rgb(251, 233, 234)",
            );

            const missedTimeline = page.locator(
              '.timeline-item.status-missed:has-text("암로디핀 5mg")',
            );
            const takenTimeline = page.locator(
              '.timeline-item.status-taken:has-text("메트포르민 500mg")',
            );
            const scheduledTimeline = page.locator(
              '.timeline-item.status-upcoming:has-text("레트로졸 2.5mg")',
            );
            await missedTimeline.waitFor();
            assert.match(await missedTimeline.innerText(), /미복용/);
            assert.match(await takenTimeline.innerText(), /완료/);
            assert.doesNotMatch(await scheduledTimeline.innerText(), /미복용/);
            assert.equal(
              await missedTimeline
                .locator(".tiny-badge.missed")
                .innerText(),
              "미복용",
            );
          },
        );
      });

      await suite.test("marks dashboard data stale and disables writes after polling fails", async () => {
        let dashboardRequestCount = 0;

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            if (pathname.endsWith("/dashboard")) {
              dashboardRequestCount += 1;
              if (dashboardRequestCount > 1) {
                await fulfillJson(
                  route,
                  failure("홈 데이터 동기화 실패", "NETWORK_ERROR"),
                  503,
                );
                return;
              }
            }
            await fulfillJson(route, responseFor(pathname));
          },
          async (page) => {
            const advanceButton = page.getByRole("button", {
              name: "30분 진행",
            });
            await advanceButton.waitFor();
            assert.equal(await advanceButton.isDisabled(), false);

            await page
              .getByText("홈 데이터 갱신 실패")
              .waitFor({ timeout: 8_000 });
            assert.equal(await advanceButton.isDisabled(), true);
            assert.match(
              await page.locator(".runtime-warning").innerText(),
              /홈 데이터 동기화 실패/,
            );
          },
        );
      });

      await suite.test("does not keep healthy badges after status polling fails", async () => {
        let statusRequestCount = 0;

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            if (pathname.endsWith("/status")) {
              statusRequestCount += 1;
              if (statusRequestCount > 1) {
                await fulfillJson(
                  route,
                  failure("상태 API 연결 실패", "NETWORK_ERROR"),
                  503,
                );
                return;
              }
            }
            await fulfillJson(route, responseFor(pathname));
          },
          async (page) => {
            await page.getByText("Backend 정상", { exact: true }).waitFor();
            await page
              .getByText("Backend 연결 안 됨", { exact: true })
              .waitFor({ timeout: 15_000 });
            await page
              .getByText("AI Server 확인 불가", { exact: true })
              .waitFor();
            await page.getByText("서버 상태 확인 실패").waitFor();
            assert.equal(
              await page.getByText("Backend 정상", { exact: true }).count(),
              0,
            );
            assert.equal(
              await page
                .getByText("AI Server 정상", { exact: true })
                .count(),
              0,
            );
          },
        );
      });

      await suite.test("keeps one optimistic bubble and reuses request_id on retry", async () => {
        let releaseFirstRequest;
        const firstRequestGate = new Promise((resolve) => {
          releaseFirstRequest = resolve;
        });
        const chatRequests = [];

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            if (pathname.endsWith("/chat/stream")) {
              const payload = JSON.parse(route.request().postData());
              chatRequests.push(payload);
              if (chatRequests.length === 1) {
                await firstRequestGate;
                await fulfillJson(
                  route,
                  failure(
                    "AI Server가 응답하지 않았습니다.",
                    "AI_UNREACHABLE",
                  ),
                  502,
                );
                return;
              }
              await fulfillChatStream(route, chatSuccess(payload.request_id));
              return;
            }
            if (pathname.endsWith("/dashboard")) {
              await fulfillJson(
                route,
                success({
                  ...dashboard,
                  clock: {
                    ...dashboard.clock,
                    current_time: "2026-04-20T09:00:00+09:00",
                  },
                }),
              );
              return;
            }
            await fulfillJson(route, responseFor(pathname));
          },
          async (page) => {
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
            await page.getByRole("tab", { name: "Chat" }).click();
            const composer = page.locator("#chat-message-input");
            const messageText = "오늘 복약 일정 알려줘";
            await composer.fill(messageText);
            await page
              .getByRole("button", { name: "메시지 보내기" })
              .click();

            const optimistic = page.locator(
              '.chat-message.user[data-delivery-status="sending"]',
            );
            await optimistic.waitFor();
            await page
              .getByText("처리중입니다", { exact: true })
              .waitFor();
            const clientMessageId =
              await optimistic.getAttribute("data-message-id");
            assert.match(clientMessageId, /^client:/);
            assert.equal(await composer.inputValue(), "");
            assert.equal(
              await optimistic.evaluate(
                (element) =>
                  element
                    .closest("section")
                    ?.querySelector(".date-divider time")
                    ?.getAttribute("datetime"),
              ),
              "2026-04-20",
            );
            assert.equal(
              await page.getByText(messageText, { exact: true }).count(),
              1,
            );

            releaseFirstRequest();
            const failed = page.locator(
              `.chat-message.user[data-message-id="${clientMessageId}"][data-delivery-status="failed"]`,
            );
            await failed.waitFor();
            assert.match(
              await failed.innerText(),
              /AI Server가 응답하지 않았습니다\./,
            );

            await failed.getByRole("button", { name: "다시 시도" }).click();
            await page
              .locator(
                '.chat-message.user[data-message-id="user-public-1"][data-delivery-status="sent"]',
              )
              .waitFor();
            await page.getByText("서버에서 생성한 실제 답변입니다.").waitFor();

            assert.equal(chatRequests.length, 2);
            assert.equal(
              chatRequests[0].request_id,
              chatRequests[1].request_id,
            );
            assert.equal(
              await page.getByText(messageText, { exact: true }).count(),
              1,
            );
            assert.equal(
              await page
                .locator('[data-message-id="assistant-public-1"]')
                .count(),
              1,
            );
            assert.equal(
              await page.locator(".chat-runtime-error").count(),
              0,
            );

            const sentUser = page.locator(
              '.chat-message.user[data-message-id="user-public-1"]',
            );
            const userActions = sentUser.locator(".message-actions");
            const preciseHover = await page.evaluate(() =>
              matchMedia("(hover: hover) and (pointer: fine)").matches,
            );
            if (preciseHover) {
              await page.mouse.move(0, 0);
              assert.equal(
                await userActions.evaluate(
                  (element) => getComputedStyle(element).opacity,
                ),
                "0",
              );
            }
            await sentUser.hover();
            await page.waitForFunction(
              () =>
                getComputedStyle(
                  document.querySelector(
                    '[data-message-id="user-public-1"] .message-actions',
                  ),
                ).opacity === "1",
            );
            const userCopy = userActions.getByRole("button", {
              name: "복사하기",
            });
            assert.equal(await userCopy.getAttribute("title"), "복사하기");
            assert.equal((await userCopy.innerText()).trim(), "");
            await userCopy.click();
            assert.equal(
              await page.evaluate(() => navigator.clipboard.readText()),
              messageText,
            );
          },
        );
      });

      await suite.test(
        "keeps same-time chat turns in Backend sort_sequence order",
        async () => {
          const sameTimeHistory = {
            days: [
              {
                date: "2026-07-26",
                messages: [
                  {
                    message_id: "user_msg_00000000000000a1",
                    sort_sequence: 99,
                    role: "user",
                    source_message_id: null,
                    response_message_id: null,
                    message_type: "text",
                    message: "이전 질문",
                    content: null,
                    created_at: "2026-07-26T09:01:00+09:00",
                    processing_status: "completed",
                    opinion_submitted: false,
                    opinion_submitted_at: null,
                    reaction: null,
                  },
                  {
                    message_id: "assistant_msg_00000000000000a2",
                    sort_sequence: 100,
                    role: "assistant",
                    source_message_id: null,
                    response_message_id: null,
                    message_type: "text",
                    message: null,
                    content: {
                      message_title: null,
                      text: "이전 답변",
                      tables: null,
                      selections: null,
                      inputs: null,
                    },
                    created_at: "2026-07-26T09:01:00+09:00",
                    processing_status: "completed",
                    opinion_submitted: false,
                    opinion_submitted_at: null,
                    reaction: null,
                  },
                ],
              },
            ],
            next_before_date: null,
          };

          await withPage(
            browser,
            baseUrl,
            async (route) => {
              const pathname = new URL(route.request().url()).pathname;
              if (pathname.endsWith("/chat/history")) {
                await fulfillJson(route, success(sameTimeHistory));
                return;
              }
              if (pathname.endsWith("/chat/stream")) {
                const payload = JSON.parse(route.request().postData());
                await fulfillChatStream(route, chatSuccess(payload.request_id));
                return;
              }
              await fulfillJson(route, responseFor(pathname));
            },
            async (page) => {
              await page
                .getByText("오늘 등록된 복약 일정이 없습니다.")
                .waitFor();
              await page.getByRole("tab", { name: "Chat" }).click();
              await page.locator("#chat-message-input").fill("새 질문");
              await page
                .getByRole("button", { name: "메시지 보내기" })
                .click();
              await page
                .locator('[data-message-id="assistant-public-1"]')
                .waitFor();

              const messageIds = await page
                .locator(".chat-message[data-message-id]")
                .evaluateAll((elements) =>
                  elements.map((element) =>
                    element.getAttribute("data-message-id"),
                  ),
                );
              assert.deepEqual(messageIds, [
                "user_msg_00000000000000a1",
                "assistant_msg_00000000000000a2",
                "user-public-1",
                "assistant-public-1",
              ]);
            },
          );
        },
      );

      await suite.test(
        "renders assistant text before the SSE stream completes",
        async () => {
          const stream = createControlledChatStream();
          setChatStreamHandler(stream.handle);

          try {
            await withPage(
              browser,
              baseUrl,
              async (route) => {
                const pathname = new URL(route.request().url()).pathname;
                if (pathname.endsWith("/chat/stream")) {
                  await route.fallback();
                  return;
                }
                await fulfillJson(route, responseFor(pathname));
              },
              async (page) => {
                await page
                  .getByText("오늘 등록된 복약 일정이 없습니다.")
                  .waitFor();
                await page.getByRole("tab", { name: "Chat" }).click();

                await page
                  .locator("#chat-message-input")
                  .fill("점진적 응답을 확인해줘");
                await page
                  .getByRole("button", { name: "메시지 보내기" })
                  .click();

                await stream.started;
                await page
                  .locator(
                    '.chat-message.user[data-delivery-status="sending"]',
                  )
                  .waitFor();
                await page
                  .getByText("처리중입니다", { exact: true })
                  .waitFor();
                const pendingTimer = page.locator(
                  ".chat-pending .response-latency-pill.is-running",
                );
                await pendingTimer.waitFor();
                assert.match(
                  await pendingTimer.innerText(),
                  /첫 응답 대기 00:\d{2}\.\d\s*\+\s*응답 생성 대기/,
                );
                assert.equal(
                  await page
                    .locator('[data-message-id^="stream:"]')
                    .count(),
                  0,
                );

                stream.releaseFirstDelta();
                await stream.firstDeltaWritten;

                const partial = page.locator(
                  '.chat-message.assistant[data-message-id^="stream:"]',
                );
                await partial.waitFor();
                await partial
                  .getByText("서버에서 생성한", { exact: true })
                  .waitFor();
                assert.equal(
                  await partial.getAttribute("aria-busy"),
                  "true",
                );
                assert.equal(
                  await page
                    .getByText("처리중입니다", { exact: true })
                    .count(),
                  0,
                );
                assert.equal(
                  await page
                    .getByText("서버에서 생성한 실제 답변입니다.", {
                      exact: true,
                    })
                    .count(),
                  0,
                );
                assert.equal(
                  await page
                    .locator(
                      '[data-message-id="assistant-public-1"]',
                    )
                    .count(),
                  0,
                );
                assert.equal(
                  await partial.locator(".message-actions").count(),
                  0,
                );
                const streamingTimer = partial.locator(
                  ".response-latency-pill.is-running",
                );
                assert.equal(await streamingTimer.count(), 1);
                assert.match(
                  (await partial
                    .locator(".response-latency-tooltip")
                    .textContent()) ?? "",
                  /첫 응답 \d+\.\d초/,
                );
                assert.doesNotMatch(
                  await page.locator("body").innerText(),
                  /PRIVATE_REASONING|reasoning_content|signature/,
                );

                await page.getByRole("tab", { name: "Home" }).click();
                assert.equal(
                  await page
                    .getByRole("button", { name: "전체 리셋" })
                    .isDisabled(),
                  true,
                );
                await page.getByRole("tab", { name: "Chat" }).click();

                stream.releaseCompletion();

                const completed = page.locator(
                  '[data-message-id="assistant-public-1"]',
                );
                await completed.waitFor();
                await completed
                  .getByText(
                    "서버에서 생성한 실제 답변입니다.",
                    { exact: true },
                  )
                  .waitFor();
                assert.equal(
                  await page
                    .locator('[data-message-id^="stream:"]')
                    .count(),
                  0,
                );
                assert.equal(await completed.count(), 1);
                const completedTimer = completed.locator(
                  ".response-latency-pill.is-completed",
                );
                assert.equal(await completedTimer.count(), 1);
                assert.match(
                  await completedTimer.innerText(),
                  /첫 응답 \d+\.\d초\s*\+\s*응답 생성 \d+\.\d초\s*=\s*전체 \d+\.\d초/,
                );
                assert.equal(
                  await page
                    .locator('[data-message-id="user-public-1"]')
                    .count(),
                  1,
                );
                assert.equal(
                  await page
                    .getByRole("button", { name: "메시지 보내기" })
                    .isDisabled(),
                  false,
                );
              },
            );
          } finally {
            stream.releaseAll();
            setChatStreamHandler(null);
          }
        },
      );

      await suite.test(
        "renders a GFM table before the SSE stream completes",
        async () => {
          const intro = "복약 상태를 정리했습니다.\n\n| 시간 | 약 이름 | 상태 |\n";
          const table =
            "|---|---|---|\n| 08:00 | Metformin | 복용 |\n| 18:00 | Letrozole | 예정 |";
          const stream = createControlledChatStream({
            firstDelta: intro,
            secondDelta: table,
            completedText: `${intro}${table}`,
          });
          setChatStreamHandler(stream.handle);

          try {
            await withPage(
              browser,
              baseUrl,
              async (route) => {
                const pathname = new URL(route.request().url()).pathname;
                if (pathname.endsWith("/chat/stream")) {
                  await route.fallback();
                  return;
                }
                await fulfillJson(route, responseFor(pathname));
              },
              async (page) => {
                await page
                  .getByText("오늘 등록된 복약 일정이 없습니다.")
                  .waitFor();
                await page.getByRole("tab", { name: "Chat" }).click();
                await page.locator("#chat-message-input").fill("표로 보여줘");
                await page
                  .getByRole("button", { name: "메시지 보내기" })
                  .click();

                await stream.started;
                stream.releaseFirstDelta();
                await stream.firstDeltaWritten;

                const partial = page.locator(
                  '.chat-message.assistant[data-message-id^="stream:"]',
                );
                await partial.waitFor();
                assert.equal(
                  await partial.locator(".markdown-table-scroll").count(),
                  0,
                );

                stream.releaseSecondDelta();
                await stream.secondDeltaWritten;
                const partialTable = partial.locator(
                  ".markdown-table-scroll table",
                );
                await partialTable.waitFor();
                assert.equal(await partial.getAttribute("aria-busy"), "true");
                assert.deepEqual(
                  await partialTable.locator("thead th").allTextContents(),
                  ["시간", "약 이름", "상태"],
                );

                stream.finish();
                const completed = page.locator(
                  '[data-message-id="assistant-public-1"]',
                );
                await completed.waitFor();
                assert.equal(
                  await completed.locator(".markdown-table-scroll table").count(),
                  1,
                );
                assert.deepEqual(
                  await completed.locator("tbody tr").allTextContents(),
                  ["08:00Metformin복용", "18:00Letrozole예정"],
                );
              },
            );
          } finally {
            stream.releaseAll();
            setChatStreamHandler(null);
          }
        },
      );

      await suite.test("does not offer retry for a terminal chat error", async () => {
        let chatRequestCount = 0;

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            if (pathname.endsWith("/chat/stream")) {
              chatRequestCount += 1;
              await fulfillJson(
                route,
                failure(
                  "요청 형식을 확인해 주세요.",
                  "INVALID_REQUEST",
                  false,
                ),
                400,
              );
              return;
            }
            await fulfillJson(route, responseFor(pathname));
          },
          async (page) => {
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
            await page.getByRole("tab", { name: "Chat" }).click();
            await page
              .locator("#chat-message-input")
              .fill("재시도할 수 없는 요청");
            await page
              .getByRole("button", { name: "메시지 보내기" })
              .click();

            const failed = page.locator(
              '.chat-message.user[data-delivery-status="failed"]',
            );
            await failed.waitFor();
            assert.match(
              await failed.innerText(),
              /요청 형식을 확인해 주세요\./,
            );
            assert.equal(
              await failed
                .getByRole("button", { name: "다시 시도" })
                .count(),
              0,
            );
            await page
              .getByText(
                "요청 내용을 확인한 뒤 새 메시지로 다시 보내 주세요.",
                { exact: true },
              )
              .waitFor();
            const failedTimer = page.locator(
              ".chat-runtime-error .response-latency-pill.is-failed",
            );
            assert.equal(await failedTimer.count(), 1);
            assert.match(
              await failedTimer.innerText(),
              /실패 \d+\.\d초/,
            );
            assert.equal(chatRequestCount, 1);
          },
        );
      });

      await suite.test("replaces symptom processing state with a synchronous selection box", async () => {
        let releaseResponse;
        const responseGate = new Promise((resolve) => {
          releaseResponse = resolve;
        });
        const chatRequests = [];

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            if (pathname.endsWith("/chat/stream")) {
              const payload = JSON.parse(route.request().postData());
              chatRequests.push(payload);
              await responseGate;
              await fulfillChatStream(
                route,
                symptomSelectionSuccess(payload.request_id),
              );
              return;
            }
            await fulfillJson(route, responseFor(pathname));
          },
          async (page) => {
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
            await page.getByRole("tab", { name: "Chat" }).click();

            const symptomMessage = "어제 속이 좀 메스꺼웠어";
            await page.locator("#chat-message-input").fill(symptomMessage);
            await page
              .getByRole("button", { name: "메시지 보내기" })
              .click();

            const optimistic = page.locator(
              '.chat-message.user[data-delivery-status="sending"]',
            );
            await optimistic.waitFor();
            assert.equal(
              await optimistic.getByText(symptomMessage, { exact: true }).count(),
              1,
            );
            await page
              .getByText("처리중입니다", { exact: true })
              .waitFor();
            assert.equal(
              await page
                .getByText("지난 7일 동안 메스꺼움의 정도는 어땠나요?", {
                  exact: true,
                })
                .count(),
              0,
            );

            releaseResponse();

            const selectionMessage = page.locator(
              '[data-message-id="assistant-symptom-1"][data-message-type="selection_box"]',
            );
            await selectionMessage.waitFor();
            assert.equal(
              await page.getByText("처리중입니다", { exact: true }).count(),
              0,
            );
            await selectionMessage
              .getByText(
                "메스꺼움 증상이 약물과 관련이 있을 수 있습니다. 아래의 질문에 답변해 주시면 증상을 더 정확하게 평가할 수 있습니다.",
                { exact: true },
              )
              .waitFor();
            await selectionMessage
              .getByText("지난 7일 동안 메스꺼움의 정도는 어땠나요?", {
                exact: true,
              })
              .waitFor();
            for (const option of ["없음", "경미함", "중간 정도", "심함"]) {
              assert.equal(
                await selectionMessage
                  .getByRole("button", { name: option, exact: true })
                  .isEnabled(),
                true,
              );
            }
            assert.equal(chatRequests.length, 1);
            assert.equal(chatRequests[0].message, symptomMessage);
            assert.equal(chatRequests[0].requested_return_type, "text");
          },
        );
      });

      await suite.test("restores structured answers only through explicit message links", async () => {
        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            await fulfillJson(
              route,
              pathname.endsWith("/chat/history")
                ? success(explicitlyLinkedStructuredHistory)
                : responseFor(pathname),
            );
          },
          async (page) => {
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
            await page.getByRole("tab", { name: "Chat" }).click();

            const card = page.locator(
              '[data-message-id="assistant-linked-selection"]',
            );
            await card.waitFor();
            assert.equal(
              await card
                .getByText("정확한 선택으로 응답했습니다.", {
                  exact: true,
                })
                .count(),
              1,
            );
            assert.equal(
              await card
                .getByText("잘못된 선택으로 응답했습니다.", {
                  exact: true,
                })
                .count(),
              0,
            );
          },
        );
      });

      await suite.test("restores cross-page structured answers after Backend before_date pagination", async () => {
        const historyRequests = [];
        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const requestUrl = new URL(route.request().url());
            const pathname = requestUrl.pathname;
            if (pathname.endsWith("/chat/history")) {
              historyRequests.push({
                beforeDate: requestUrl.searchParams.get("before_date"),
                limitDays: requestUrl.searchParams.get("limit_days"),
                limitTurns: requestUrl.searchParams.get("limit_turns"),
              });
              await fulfillJson(
                route,
                success(
                  requestUrl.searchParams.get("before_date") === "2026-07-26"
                    ? crossPageLinkedPreviousHistory
                    : crossPageLinkedCurrentHistory,
                ),
              );
              return;
            }
            await fulfillJson(route, responseFor(pathname));
          },
          async (page) => {
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
            await page.getByRole("tab", { name: "Chat" }).click();

            assert.equal(
              await page
                .locator(
                  '[data-message-id="assistant-cross-page-selection"]',
                )
                .count(),
              0,
            );

            await page.locator("#chat-log").dispatchEvent("wheel", {
              deltaY: -120,
            });

            const card = page.locator(
              '[data-message-id="assistant-cross-page-selection"]',
            );
            await card.waitFor();
            assert.deepEqual(historyRequests, [
              {
                beforeDate: null,
                limitDays: "1",
                limitTurns: "50",
              },
              {
                beforeDate: "2026-07-26",
                limitDays: "1",
                limitTurns: "50",
              },
            ]);
            assert.equal(await card.count(), 1);
            assert.equal(
              await card
                .getByText("정확한 과거 응답으로 응답했습니다.", {
                  exact: true,
                })
                .count(),
              1,
            );
            assert.equal(
              await card
                .getByText("다른 응답으로 응답했습니다.", {
                  exact: true,
                })
                .count(),
              0,
            );
            assert.deepEqual(
              await page
                .locator("[data-conversation-date]")
                .evaluateAll((elements) =>
                  elements.map((element) =>
                    element.getAttribute("data-conversation-date"),
                  ),
                ),
              ["2026-07-25", "2026-07-26"],
            );
          },
        );
      });

      await suite.test("renders assistant bold markers and medication bullets as safe markup", async () => {
        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            await fulfillJson(
              route,
              pathname.endsWith("/chat/history")
                ? success(markdownAssistantHistory)
                : responseFor(pathname),
            );
          },
          async (page) => {
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
            await page.getByRole("tab", { name: "Chat" }).click();
            const assistant = page.locator(
              '[data-message-id="assistant-markdown-1"]',
            );
            await assistant.waitFor();

            assert.equal(
              await assistant.locator(".contract-message-text ul").count(),
              1,
            );
            assert.equal(
              await assistant.locator(".contract-message-text li").count(),
              5,
            );
            assert.deepEqual(
              await assistant
                .locator(".contract-message-text ol > li")
                .allTextContents(),
              ["아침 복용", "저녁 복용"],
            );
            assert.deepEqual(
              await assistant
                .locator(".contract-message-text strong")
                .allTextContents(),
              [
                "메트포르민 500mg",
                "수니티닙 50mg",
                "레트로졸 2.5mg",
                "어느 약을 복용한 후에 메스꺼움이 시작되었나요?",
                "언제부터 속이 메스꺼워졌나요?",
              ],
            );
            assert.doesNotMatch(await assistant.innerText(), /\*\*/);
            assert.equal(await assistant.locator("script").count(), 0);
            assert.equal(
              await page.evaluate(() => window.__markdownProbe ?? null),
              null,
            );
            const markdownTable = assistant.locator(
              ".contract-message-text .markdown-table-scroll table",
            );
            assert.equal(await markdownTable.count(), 1);
            assert.deepEqual(
              await markdownTable.locator("thead th").allTextContents(),
              ["Time", "Medication", "Status"],
            );
            assert.deepEqual(
              await markdownTable
                .locator("tbody tr")
                .first()
                .locator("td")
                .allTextContents(),
              ["08:00", "Metformin", "Taken"],
            );
          },
        );
      });

      await suite.test("supports mutually exclusive reactions, opinion, copy, and accessible actions", async () => {
        const feedbackRequests = [];
        let currentReaction = null;

        await withPage(
          browser,
          baseUrl,
          async (route) => {
            const pathname = new URL(route.request().url()).pathname;
            if (pathname.endsWith("/chat/history")) {
              await fulfillJson(route, success(assistantHistory));
              return;
            }
            if (pathname.endsWith("/chat/feedback")) {
              const payload = JSON.parse(route.request().postData());
              feedbackRequests.push(payload);
              if (payload.reaction) {
                currentReaction = payload.reaction;
                await fulfillJson(
                  route,
                  success({
                    status: "accepted",
                    request_id: payload.request_id,
                    assistant_message_id: payload.assistant_message_id,
                    reaction: currentReaction,
                    opinion_submitted: false,
                    opinion_submitted_at: null,
                  }),
                );
                return;
              }
              await fulfillJson(
                route,
                success({
                  status: "accepted",
                  request_id: payload.request_id,
                  assistant_message_id: payload.assistant_message_id,
                  reaction: currentReaction,
                  opinion_submitted: true,
                  opinion_submitted_at: payload.feedback_at,
                }),
              );
              return;
            }
            await fulfillJson(route, responseFor(pathname));
          },
          async (page) => {
            await page
              .getByText("오늘 등록된 복약 일정이 없습니다.")
              .waitFor();
            await page.getByRole("tab", { name: "Chat" }).click();
            const assistant = page.locator(
              '[data-message-id="assistant-history-1"]',
            );
            const actions = assistant.locator(".message-actions");
            await assistant.waitFor();

            const preciseHover = await page.evaluate(() =>
              matchMedia("(hover: hover) and (pointer: fine)").matches,
            );
            if (preciseHover) {
              assert.equal(
                await actions.evaluate(
                  (element) => getComputedStyle(element).opacity,
                ),
                "0",
              );
            }
            await assistant.hover();
            await page.waitForFunction(
              () =>
                getComputedStyle(
                  document.querySelector(
                    '[data-message-id="assistant-history-1"] .message-actions',
                  ),
                ).opacity === "1",
            );

            const like = actions.getByRole("button", { name: "좋아요" });
            const dislike = actions.getByRole("button", { name: "싫어요" });
            const copy = actions.getByRole("button", { name: "복사하기" });
            assert.equal(await like.getAttribute("title"), "좋아요");
            assert.equal(await dislike.getAttribute("title"), "싫어요");
            assert.equal(await copy.getAttribute("title"), "복사하기");
            assert.equal((await like.innerText()).includes("좋아요"), false);
            assert.equal((await dislike.innerText()).includes("싫어요"), false);
            assert.equal((await copy.innerText()).trim(), "");
            await like.click();
            assert.equal(await like.getAttribute("aria-pressed"), "true");
            assert.equal(await dislike.getAttribute("aria-pressed"), "false");

            await dislike.click();
            assert.equal(await like.getAttribute("aria-pressed"), "false");
            assert.equal(await dislike.getAttribute("aria-pressed"), "true");
            const reactionRequestCount = feedbackRequests.length;
            await dislike.click();
            assert.equal(feedbackRequests.length, reactionRequestCount);
            assert.deepEqual(
              feedbackRequests
                .filter((payload) => payload.reaction)
                .map((payload) => payload.reaction),
              ["like", "dislike"],
            );
            assert.ok(
              feedbackRequests
                .filter((payload) => payload.reaction)
                .every(
                  (payload) =>
                    payload.assistant_message_id ===
                      "assistant-history-1" &&
                    typeof payload.request_id === "string" &&
                    typeof payload.feedback_at === "string",
                ),
            );

            await page.mouse.move(0, 0);
            await copy.focus();
            assert.equal(
              await actions.evaluate(
                (element) => getComputedStyle(element).opacity,
              ),
              "1",
            );
            await copy.click();
            const neutralToast = page.locator(
              '#app-toast[data-kind="neutral"]',
            );
            await neutralToast.waitFor();
            assert.equal(
              await neutralToast.evaluate(
                (element) => getComputedStyle(element).backgroundColor,
              ),
              "rgb(238, 240, 242)",
            );
            assert.match(
              await page.evaluate(() => navigator.clipboard.readText()),
              /복약 안내\r?\n서버에서 받은 실제 답변입니다\./,
            );

            await actions
              .getByRole("button", { name: "의견 남기기" })
              .click();
            await assistant
              .getByRole("button", { name: "의견 보내기" })
              .click();
            const warningToast = page.locator(
              '#app-toast[data-kind="warning"]',
            );
            await warningToast.waitFor();
            assert.match(
              await warningToast.evaluate(
                (element) => getComputedStyle(element).backgroundColor,
              ),
              /^rgb\(238, 240, 242\)$/,
            );
            const opinion = "설명이 명확해서 이해하기 쉬웠습니다.";
            await assistant.locator(".inline-feedback-form textarea").fill(
              opinion,
            );
            await assistant
              .getByRole("button", { name: "의견 보내기" })
              .click();
            await assistant.getByText("의견 전송됨").waitFor();
            const opinionPayload = feedbackRequests.find(
              (payload) => payload.opinion_text,
            );
            assert.equal(opinionPayload.opinion_text, opinion);
            assert.equal(
              opinionPayload.assistant_message_id,
              "assistant-history-1",
            );
            assert.equal(opinionPayload.reaction, undefined);
            assert.equal(typeof opinionPayload.feedback_at, "string");
          },
        );
      });
    } finally {
      await browser.close();
      await closeServer(server);
    }
  },
);

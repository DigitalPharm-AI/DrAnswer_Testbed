"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright-core");
const {
  apiPath,
  assert,
  assertCompletedSseContract,
  assistantPhraseViolations,
  backendNutritionEvidence,
  check,
  collectPayloadViolations,
  diagnosticError,
  isMealApprovalCard,
  isStructuredChatRequest,
  parseSseEvents,
  publicEnvelopeData,
  requestBody,
  safeJsonParse,
} = require("./playwright/validation_helpers");
const {
  advanceMealInteractionToApproval,
} = require("./playwright/scenarios/meal");
const {
  runProCtcaeStructuredRegression,
} = require("./playwright/scenarios/pro_ctcae");

const baseUrl = (process.env.BASE_URL || "http://127.0.0.1:9000").replace(
  /\/$/,
  "",
);
const applicationOrigin = new URL(baseUrl).origin;
const outputRoot =
  process.env.REAL_LLM_SERVICE_OUTPUT_DIR ||
  path.join("output", "playwright", "p0-real-service-manual");
const artifactDir = path.join(outputRoot, "p0-real-service-validation");
const chromePath =
  process.env.CHROME_PATH ||
  "C:/Program Files/Google/Chrome/Application/chrome.exe";
const llmTimeoutMs = Number(process.env.REAL_LLM_CHAT_TIMEOUT_MS || 135_000);
const deterministicV13Mode =
  process.env.P0_REAL_SERVICE_MODE === "deterministic_v13";
const ownedAgentProcessId = Number(process.env.AGENT_PROCESS_PID || 0);
const intentionallyAbortedRequestIds = new Set();

const mealPrompt =
  "오늘 아침 08:30에 현미밥 150g, 두부된장국 1그릇, 시금치나물 1접시를 먹었습니다. 이 아침 식사를 기록해 주세요. 실제 저장 전에는 기록과 취소 확인 카드를 보여 주세요.";

function ensureDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true });
}

function writeJson(fileName, value) {
  fs.writeFileSync(
    path.join(artifactDir, fileName),
    `${JSON.stringify(value, null, 2)}\n`,
    "utf8",
  );
}

async function screenshot(page, fileName) {
  await page.screenshot({
    path: path.join(artifactDir, fileName),
    fullPage: true,
  });
}

async function pollUntil(action, predicate, options = {}) {
  const timeoutMs = options.timeoutMs || 15_000;
  const intervalMs = options.intervalMs || 200;
  const deadline = Date.now() + timeoutMs;
  let lastValue;
  let lastError;
  while (Date.now() < deadline) {
    try {
      lastValue = await action();
      if (predicate(lastValue)) {
        return lastValue;
      }
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw diagnosticError(
    options.code || "POLL_TIMEOUT",
    options.message || "The expected state did not become available.",
    {
      lastValue,
      lastError: lastError?.message || null,
      timeoutMs,
    },
  );
}

async function capturedChatStream(page, requestId, timeoutMs = llmTimeoutMs) {
  const captured = await pollUntil(
    () =>
      page.evaluate((expectedRequestId) => {
        const streams = Array.isArray(
          globalThis.__codexCapturedChatStreams,
        )
          ? globalThis.__codexCapturedChatStreams
          : [];
        const entry = [...streams]
          .reverse()
          .find((item) => item.request_id === expectedRequestId);
        return entry
          ? {
              request_id: entry.request_id,
              status: entry.status,
              content_type: entry.content_type,
              body: entry.body,
              error: entry.error,
              done: entry.done,
            }
          : null;
      }, requestId),
    (entry) => entry?.done === true || typeof entry?.error === "string",
    {
      timeoutMs,
      code: "UI_SSE_CAPTURE_TIMEOUT",
      message:
        "The browser did not finish consuming the correlated Backend SSE stream.",
    },
  );
  assert(
    captured.error === null &&
      typeof captured.body === "string" &&
      captured.body.length > 0,
    "UI_SSE_CAPTURE_FAILED",
    "The browser could not capture the correlated Backend SSE stream.",
    { requestId, captured },
  );
  return captured;
}

async function sendDeterministicChat(page, prompt) {
  const composer = page.getByRole("textbox", {
    name: "AI 에이전트에게 질문하기",
  });
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  let streamFinished = false;
  const requestPromise = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      apiPath(request.url()) === "/api/ui/v1/chat/stream" &&
      requestBody(request)?.message === prompt,
    { timeout: llmTimeoutMs },
  );
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      apiPath(response.url()) === "/api/ui/v1/chat/stream" &&
      requestBody(response.request())?.message === prompt,
    { timeout: llmTimeoutMs },
  );
  await page.getByRole("button", { name: "메시지 보내기" }).click();
  const request = await requestPromise;
  const requestPayload = requestBody(request);
  const requestId = requestPayload?.request_id;
  assert(
    typeof requestId === "string" && requestId.length > 0,
    "UI_CHAT_REQUEST_ID_MISSING",
    "The UI chat request did not contain a public request_id.",
    { requestPayload },
  );
  const optimisticMessageId = `client:${requestId}`;
  const optimisticCard = page.locator(
    `.chat-message.user[data-message-id=${JSON.stringify(
      optimisticMessageId,
    )}]`,
  );
  await optimisticCard.waitFor({ state: "visible", timeout: 5_000 });
  const optimisticState = {
    message_id: optimisticMessageId,
    delivery_status:
      await optimisticCard.getAttribute("data-delivery-status"),
    text: (await optimisticCard.innerText()).trim(),
    stream_finished: streamFinished,
  };
  assert(
    optimisticState.delivery_status === "sending" &&
      optimisticState.text.includes(prompt) &&
      optimisticState.stream_finished === false,
    "OPTIMISTIC_USER_MESSAGE_NOT_IMMEDIATE",
    "The user message was not visible in sending state before the terminal AI event.",
    optimisticState,
  );

  const response = await responsePromise;
  assert(
    response.status() === 200 &&
      (response.headers()["content-type"] || "")
        .toLowerCase()
        .includes("text/event-stream"),
    "UI_CHAT_STREAM_RESPONSE_INVALID",
    "The React chat request did not receive a successful Backend SSE stream.",
    {
      status: response.status(),
      content_type: response.headers()["content-type"] || "",
    },
  );
  const captured = await capturedChatStream(page, requestId);
  const events = parseSseEvents(captured.body);
  streamFinished = true;
  const streamPayloadViolations = collectPayloadViolations(
    events,
    "POST /api/ui/v1/chat/stream response",
  );
  assert(
    streamPayloadViolations.length === 0,
    "UI_SSE_PRIVATE_PAYLOAD_EXPOSURE",
    "The Backend UI stream exposed a private patient or internal identifier.",
    { requestId, streamPayloadViolations },
  );
  const { completed, deltas } = assertCompletedSseContract(
    events,
    requestId,
  );
  const persistedUserCard = page.locator(
    `.chat-message.user[data-message-id=${JSON.stringify(
      completed.user_message_id,
    )}]`,
  );
  const assistantCard = page.locator(
    `.chat-message.assistant[data-message-id=${JSON.stringify(
      completed.assistant_message_id,
    )}]`,
  );
  await persistedUserCard.waitFor({ state: "visible" });
  await assistantCard.waitFor({ state: "visible" });
  assert(
    (await optimisticCard.count()) === 0,
    "OPTIMISTIC_USER_MESSAGE_NOT_RECONCILED",
    "The temporary user message ID remained after the Backend completed event.",
    { optimisticMessageId, completed },
  );
  return {
    request: requestPayload,
    response_status: response.status(),
    events,
    completed,
    delta_count: deltas.length,
    assistant_text: (await assistantCard.innerText()).trim(),
    optimistic: optimisticState,
    assistant_card: assistantCard,
  };
}

async function replayDeterministicChat(page, originalChat) {
  const result = await page.evaluate(async (body) => {
    const response = await fetch("/api/ui/v1/chat/stream", {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    return {
      status: response.status,
      content_type: response.headers.get("content-type") || "",
      body: await response.text(),
    };
  }, originalChat.request);
  assert(
    result.status === 200 &&
      result.content_type.toLowerCase().includes("text/event-stream"),
    "UI_CHAT_REPLAY_STREAM_INVALID",
    "The identical UI chat request did not return a successful SSE stream.",
    result,
  );
  const events = parseSseEvents(result.body);
  const { completed, deltas } = assertCompletedSseContract(
    events,
    originalChat.request.request_id,
  );
  assert(
    completed.user_message_id === originalChat.completed.user_message_id &&
      completed.assistant_message_id ===
        originalChat.completed.assistant_message_id,
    "UI_CHAT_REPLAY_RESULT_CHANGED",
    "The same request_id did not replay the original persisted messages.",
    {
      original: originalChat.completed,
      replay: completed,
    },
  );
  return {
    request_id: originalChat.request.request_id,
    user_message_id: completed.user_message_id,
    assistant_message_id: completed.assistant_message_id,
    event_types: events.map((event) => event.type),
    delta_count: deltas.length,
  };
}

async function abortAfterStartAndReplay(page) {
  const request = {
    request_id: "req_aaaaaaaaaaaaaaaa",
    requested_return_type: "text",
    message: "연결 중단 후 동일 요청 재시도 테스트",
  };
  intentionallyAbortedRequestIds.add(request.request_id);
  const aborted = await page.evaluate(async (body) => {
    const controller = new AbortController();
    const response = await fetch("/api/ui/v1/chat/stream", {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let received = "";
    while (!received.includes("event: start")) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      received += decoder.decode(chunk.value, { stream: true });
    }
    controller.abort();
    await reader.cancel().catch(() => {});
    return {
      status: response.status,
      content_type: response.headers.get("content-type") || "",
      received,
    };
  }, request);
  assert(
    aborted.status === 200 &&
      aborted.content_type.toLowerCase().includes("text/event-stream") &&
      aborted.received.includes("event: start"),
    "UI_CHAT_ABORT_START_MISSING",
    "The browser could not abort the chat stream immediately after SSE start.",
    aborted,
  );

  await page.waitForTimeout(4_000);
  const replay = await page.evaluate(async (body) => {
    const response = await fetch("/api/ui/v1/chat/stream", {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    return {
      status: response.status,
      content_type: response.headers.get("content-type") || "",
      body: await response.text(),
    };
  }, request);
  assert(
    replay.status === 200 &&
      replay.content_type.toLowerCase().includes("text/event-stream"),
    "UI_CHAT_ABORT_REPLAY_STREAM_INVALID",
    "The disconnected chat could not be retried with the same request_id.",
    replay,
  );
  const events = parseSseEvents(replay.body);
  const { completed } = assertCompletedSseContract(
    events,
    request.request_id,
  );
  return {
    request_id: request.request_id,
    user_message_id: completed.user_message_id,
    assistant_message_id: completed.assistant_message_id,
    aborted_after_start: true,
    replay_event_types: events.map((event) => event.type),
  };
}

async function runConcurrentSameRequest(page) {
  const request = {
    request_id: "req_bbbbbbbbbbbbbbbb",
    requested_return_type: "text",
    message: "동일 요청을 동시에 보내는 테스트",
  };
  const responses = await page.evaluate(async (body) => {
    const send = async () => {
      const response = await fetch("/api/ui/v1/chat/stream", {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      });
      return {
        status: response.status,
        content_type: response.headers.get("content-type") || "",
        body: await response.text(),
      };
    };
    return Promise.all([send(), send()]);
  }, request);
  const parsed = responses.map((response) => {
    assert(
      response.status === 200 &&
        response.content_type.toLowerCase().includes("text/event-stream"),
      "UI_CHAT_CONCURRENT_STREAM_INVALID",
      "A concurrent duplicate request did not return an SSE stream.",
      response,
    );
    const events = parseSseEvents(response.body);
    const terminals = events.filter((event) =>
      ["completed", "error"].includes(event.type),
    );
    assert(
      events[0]?.type === "start" &&
        terminals.length === 1 &&
        events.at(-1) === terminals[0],
      "UI_CHAT_CONCURRENT_TERMINAL_INVALID",
      "Each concurrent duplicate stream must have exactly one terminal event.",
      { events },
    );
    return { events, terminal: terminals[0] };
  });
  const completed = parsed
    .map((item) => item.terminal)
    .filter((terminal) => terminal.type === "completed");
  const errors = parsed
    .map((item) => item.terminal)
    .filter((terminal) => terminal.type === "error");
  assert(
    completed.length >= 1 &&
      completed.every(
        (terminal) =>
          terminal.data?.assistant_message_id ===
          completed[0].data?.assistant_message_id,
      ) &&
      errors.every((terminal) => terminal.data?.retryable === true),
    "UI_CHAT_CONCURRENT_RESULT_INVALID",
    "Concurrent duplicate requests did not converge on one persisted result.",
    { parsed },
  );
  return {
    request_id: request.request_id,
    completed_count: completed.length,
    error_count: errors.length,
    assistant_message_id: completed[0].data.assistant_message_id,
    terminal_types: parsed.map((item) => item.terminal.type),
  };
}

async function runFailureAfterToken(page) {
  const marker = "[stream-failure-after-token]";
  const request = {
    request_id: "req_cccccccccccccccc",
    requested_return_type: "text",
    message: `토큰 공개 후 오류 테스트 ${marker}`,
  };
  const response = await page.evaluate(async (body) => {
    const result = await fetch("/api/ui/v1/chat/stream", {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    return {
      status: result.status,
      content_type: result.headers.get("content-type") || "",
      body: await result.text(),
    };
  }, request);
  assert(
    response.status === 200 &&
      response.content_type.toLowerCase().includes("text/event-stream"),
    "UI_CHAT_FAILURE_AFTER_TOKEN_STREAM_INVALID",
    "The failure-after-token request did not return an SSE stream.",
    response,
  );
  const events = parseSseEvents(response.body);
  const deltas = events.filter((event) => event.type === "text_delta");
  const terminals = events.filter((event) =>
    ["completed", "error"].includes(event.type),
  );
  assert(
    events[0]?.type === "start" &&
      deltas.length >= 1 &&
      deltas.map((event) => event.data?.text || "").join("") ===
        "테스트 부분 응답" &&
      terminals.length === 1 &&
      terminals[0].type === "error" &&
      terminals[0].data?.retryable === true &&
      events.at(-1) === terminals[0],
    "UI_CHAT_FAILURE_AFTER_TOKEN_CONTRACT_INVALID",
    "A token-level failure must retain deltas and end with one retryable error.",
    { events },
  );
  const retry = await page.evaluate(async (body) => {
    const result = await fetch("/api/ui/v1/chat/stream", {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    return {
      status: result.status,
      content_type: result.headers.get("content-type") || "",
      body: await result.text(),
    };
  }, request);
  assert(
    retry.status === 200 &&
      retry.content_type.toLowerCase().includes("text/event-stream"),
    "UI_CHAT_FAILURE_AFTER_TOKEN_RETRY_STREAM_INVALID",
    "The retryable failed stream could not reuse the same request_id.",
    retry,
  );
  const retryEvents = parseSseEvents(retry.body);
  const { completed } = assertCompletedSseContract(
    retryEvents,
    request.request_id,
  );
  return {
    request_id: request.request_id,
    delta_text: deltas.map((event) => event.data.text).join(""),
    failed_event_types: events.map((event) => event.type),
    first_error: terminals[0].data,
    retry_event_types: retryEvents.map((event) => event.type),
    retry_user_message_id: completed.user_message_id,
    retry_assistant_message_id: completed.assistant_message_id,
  };
}

async function runSlowConsumerBackpressure(page) {
  const marker = "[burst-token-stream]";
  const expectedChunkCount = 270;
  const expectedChunkSize = 512;
  const request = {
    request_id: "req_dddddddddddddddd",
    requested_return_type: "text",
    message: `느린 SSE 소비자 테스트 ${marker}`,
  };
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false,
    latency: 100,
    downloadThroughput: 64 * 1024,
    uploadThroughput: 64 * 1024,
    connectionType: "cellular3g",
  });
  const startedAt = Date.now();
  let response;
  try {
    response = await page.evaluate(async (body) => {
      const result = await fetch("/api/ui/v1/chat/stream", {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      });
      return {
        status: result.status,
        content_type: result.headers.get("content-type") || "",
        body: await result.text(),
      };
    }, request);
  } finally {
    await cdp.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
      connectionType: "none",
    });
    await cdp.detach();
  }
  const elapsedMs = Date.now() - startedAt;
  assert(
    response.status === 200 &&
      response.content_type.toLowerCase().includes("text/event-stream"),
    "UI_CHAT_SLOW_CONSUMER_STREAM_INVALID",
    "The throttled browser did not receive a successful SSE stream.",
    response,
  );
  const events = parseSseEvents(response.body);
  const { completed, deltas } = assertCompletedSseContract(
    events,
    request.request_id,
  );
  const combined = deltas
    .map((event) => event.data?.text || "")
    .join("");
  assert(
    deltas.length === expectedChunkCount &&
      deltas.every(
        (event) => event.data.text.length === expectedChunkSize,
      ) &&
      completed.message?.text === combined &&
      elapsedMs >= 2_000,
    "UI_CHAT_SLOW_CONSUMER_DATA_LOSS",
    "The bounded Backend stream lost, reordered, or duplicated burst tokens.",
    {
      expectedChunkCount,
      actualChunkCount: deltas.length,
      expectedChunkSize,
      combinedLength: combined.length,
      completedLength: completed.message?.text?.length || 0,
      elapsedMs,
    },
  );
  return {
    request_id: request.request_id,
    user_message_id: completed.user_message_id,
    assistant_message_id: completed.assistant_message_id,
    delta_count: deltas.length,
    total_characters: combined.length,
    elapsed_ms: elapsedMs,
    terminal_type: events.at(-1).type,
  };
}

async function submitStructuredSelection(page, sourceCard, selection) {
  const sourceMessageId = await sourceCard.getAttribute("data-message-id");
  const sourceMessageType = await sourceCard.getAttribute("data-message-type");
  assert(
    typeof sourceMessageId === "string" && sourceMessageId.length > 0,
    "STRUCTURED_SOURCE_MESSAGE_ID_MISSING",
    "The structured assistant card did not expose its public message ID.",
    { selection, sourceMessageType },
  );
  assert(
    sourceMessageType === "selection_box",
    "STRUCTURED_SOURCE_TYPE_INVALID",
    "A selection response can only be submitted from a selection_box card.",
    { sourceMessageId, sourceMessageType, selection },
  );

  const requestPromise = page.waitForRequest(
    (request) => {
      const body = requestBody(request);
      return (
        request.method() === "POST" &&
        apiPath(request.url()) === "/api/ui/v1/chat/stream" &&
        body?.message === selection &&
        body?.requested_return_type === "selection_box" &&
        body?.source_message_id === sourceMessageId
      );
    },
    { timeout: llmTimeoutMs },
  );
  const responsePromise = page.waitForResponse(
    (response) => {
      const body = requestBody(response.request());
      return (
        response.request().method() === "POST" &&
        apiPath(response.url()) === "/api/ui/v1/chat/stream" &&
        body?.message === selection &&
        body?.source_message_id === sourceMessageId
      );
    },
    { timeout: llmTimeoutMs },
  );

  await sourceCard
    .getByRole("button", { name: selection, exact: true })
    .click();
  const request = await requestPromise;
  const requestPayload = requestBody(request);
  const response = await responsePromise;
  assert(
    response.status() === 200 &&
      (response.headers()["content-type"] || "")
        .toLowerCase()
        .includes("text/event-stream"),
    "STRUCTURED_STREAM_RESPONSE_INVALID",
    "The structured response did not receive a successful Backend SSE stream.",
    { sourceMessageId, selection, status: response.status() },
  );

  const captured = await capturedChatStream(page, requestPayload.request_id);
  const events = parseSseEvents(captured.body);
  const { completed, deltas } = assertCompletedSseContract(
    events,
    requestPayload.request_id,
  );
  const assistantCard = page.locator(
    `.chat-message.assistant[data-message-id=${JSON.stringify(
      completed.assistant_message_id,
    )}]`,
  );
  await assistantCard.waitFor({ state: "visible" });
  return {
    request: requestPayload,
    events,
    completed,
    delta_count: deltas.length,
    assistant_card: assistantCard,
    assistant_text: (await assistantCard.innerText()).trim(),
  };
}

async function dashboardData(page) {
  const result = await page.evaluate(async () => {
    const response = await fetch("/api/ui/v1/dashboard", {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    return {
      status: response.status,
      payload: await response.json(),
    };
  });
  const data = publicEnvelopeData(result.payload);
  assert(
    result.status === 200 && data,
    "DASHBOARD_STATE_ORACLE_FAILED",
    "The isolated Backend dashboard state could not be read.",
    result,
  );
  return data;
}

function processIsRunning(processId) {
  try {
    process.kill(processId, 0);
    return true;
  } catch {
    return false;
  }
}

async function terminateOwnedAgentProcess() {
  assert(
    Number.isInteger(ownedAgentProcessId) &&
      ownedAgentProcessId > 0 &&
      processIsRunning(ownedAgentProcessId),
    "OWNED_AGENT_PROCESS_MISSING",
    "The isolated runner did not provide a live Agent process to stop.",
    { ownedAgentProcessId },
  );
  process.kill(ownedAgentProcessId, "SIGTERM");
  await pollUntil(
    () => processIsRunning(ownedAgentProcessId),
    (running) => running === false,
    {
      timeoutMs: 10_000,
      code: "OWNED_AGENT_PROCESS_DID_NOT_STOP",
      message: "The runner-owned Agent process did not stop for the failure test.",
    },
  );
}

async function runDeterministicV13Core(page, report) {
  const readyNotification = await pollUntil(
    () => dashboardData(page),
    (dashboard) =>
      dashboard.notifications?.some(
        (notification) =>
          notification.interaction?.kind === "open_chat" &&
          notification.interaction?.state === "ready" &&
          typeof notification.interaction?.message_id === "string",
      ),
    {
      timeoutMs: 60_000,
      intervalMs: 500,
      code: "MISSED_DOSE_ASYNC_CALLBACK_NOT_READY",
      message:
        "The missed-dose request did not complete through the Agent worker callback.",
    },
  ).then((dashboard) =>
    dashboard.notifications.find(
      (notification) =>
        notification.interaction?.kind === "open_chat" &&
        notification.interaction?.state === "ready" &&
        typeof notification.interaction?.message_id === "string",
    ),
  );
  assert(
    readyNotification,
    "MISSED_DOSE_NOTIFICATION_MISSING",
    "The ready missed-dose notification could not be resolved.",
  );

  await page.reload({ waitUntil: "domcontentloaded" });
  const openChatButton = page
    .getByRole("button", { name: "대화 확인", exact: true })
    .first();
  await openChatButton.waitFor({ state: "visible", timeout: 20_000 });
  assert(
    await openChatButton.isEnabled(),
    "MISSED_DOSE_CHAT_BUTTON_NOT_READY",
    "The ready missed-dose alert did not expose an enabled chat action.",
    { readyNotification },
  );
  await openChatButton.click();
  const missedDoseCard = page.locator(
    `.chat-message.assistant[data-message-id=${JSON.stringify(
      readyNotification.interaction.message_id,
    )}]`,
  );
  await missedDoseCard.waitFor({ state: "visible" });
  const missedDoseText = (await missedDoseCard.innerText()).trim();
  assert(
    missedDoseText.includes(readyNotification.body) &&
      missedDoseText.includes("복약을 놓친"),
    "MISSED_DOSE_CHAT_MESSAGE_MISMATCH",
    "The alert did not open the Agent-generated missed-dose chat message.",
    { readyNotification, missedDoseText },
  );
  report.missed_dose = {
    notification_id: readyNotification.id,
    dose_event_id: readyNotification.related_dose_event_id,
    message_id: readyNotification.interaction.message_id,
    message: readyNotification.body,
  };
  check(report, "missed_dose_async_follow_up_chat", report.missed_dose);
  await screenshot(page, "04-v13-missed-dose-chat.png");

  const chat = await sendDeterministicChat(
    page,
    "지금은 복용할 수 있어요. 다음에 무엇을 확인하면 될까요?",
  );
  assert(
    chat.delta_count > 0,
    "AI_NDJSON_DELTA_MISSING",
    "The real Backend-to-Agent NDJSON path did not produce final-answer text deltas.",
    { events: chat.events },
  );
  assert(
    assistantPhraseViolations(chat.assistant_text).length === 0,
    "FORBIDDEN_ASSISTANT_PHRASE",
    "The deterministic Agent answer contained a legacy fallback phrase.",
    { assistant_text: chat.assistant_text },
  );
  report.v13_chat = {
    request: chat.request,
    response_status: chat.response_status,
    completed: chat.completed,
    event_types: chat.events.map((event) => event.type),
    delta_count: chat.delta_count,
    optimistic: chat.optimistic,
  };
  check(report, "v13_ndjson_sync_chat", report.v13_chat);
  check(report, "optimistic_user_message", chat.optimistic);

  report.v13_chat_replay = await replayDeterministicChat(page, chat);
  check(
    report,
    "v13_same_request_id_replays_persisted_messages",
    report.v13_chat_replay,
  );

  report.abort_replay = await abortAfterStartAndReplay(page);
  check(
    report,
    "browser_abort_then_same_request_id_replay",
    report.abort_replay,
  );

  report.concurrent_same_request = await runConcurrentSameRequest(page);
  check(
    report,
    "concurrent_same_request_id_converges",
    report.concurrent_same_request,
  );

  report.failure_after_token = await runFailureAfterToken(page);
  check(
    report,
    "token_error_then_same_request_retry_completes",
    report.failure_after_token,
  );

  await runProCtcaeStructuredRegression(page, report, {
    screenshot,
    sendDeterministicChat,
    submitStructuredSelection,
  });

  await chat.assistant_card.hover();
  const likeButton = chat.assistant_card.getByRole("button", {
    name: "좋아요",
    exact: true,
  });
  const likeResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      apiPath(response.url()) === "/api/ui/v1/chat/feedback" &&
      requestBody(response.request())?.assistant_message_id ===
        chat.completed.assistant_message_id &&
      requestBody(response.request())?.reaction === "like",
  );
  await likeButton.click();
  const likeResponse = await likeResponsePromise;
  const likePressed = await pollUntil(
    () => likeButton.getAttribute("aria-pressed"),
    (value) => value === "true",
    {
      timeoutMs: 5_000,
      code: "CHAT_REACTION_UI_NOT_RECONCILED",
      message:
        "The accepted assistant reaction was not reflected by the React UI.",
    },
  );
  assert(
    likeResponse.status() === 202 && likePressed === "true",
    "CHAT_REACTION_NOT_ACCEPTED",
    "The assistant like reaction was not accepted and reflected by the UI.",
    {
      status: likeResponse.status(),
      request: requestBody(likeResponse.request()),
    },
  );

  await chat.assistant_card
    .getByRole("button", { name: "의견 남기기", exact: true })
    .click();
  const opinionText = "결정적 v1.3 통합 테스트 의견입니다.";
  await chat.assistant_card
    .getByLabel(/이 답변에 대한 의견을 알려주세요/)
    .fill(opinionText);
  const opinionResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      apiPath(response.url()) === "/api/ui/v1/chat/feedback" &&
      requestBody(response.request())?.assistant_message_id ===
        chat.completed.assistant_message_id &&
      requestBody(response.request())?.opinion_text === opinionText,
  );
  await chat.assistant_card
    .getByRole("button", { name: "의견 보내기", exact: true })
    .click();
  const opinionResponse = await opinionResponsePromise;
  await chat.assistant_card
    .getByText("의견 전송됨", { exact: true })
    .waitFor({ state: "visible" });
  assert(
    opinionResponse.status() === 202,
    "CHAT_OPINION_NOT_ACCEPTED",
    "The assistant opinion was not accepted through the Agent feedback boundary.",
    {
      status: opinionResponse.status(),
      request: requestBody(opinionResponse.request()),
    },
  );
  report.feedback = {
    assistant_message_id: chat.completed.assistant_message_id,
    reaction_status: likeResponse.status(),
    opinion_status: opinionResponse.status(),
  };
  check(report, "chat_feedback_boundary", report.feedback);
  await screenshot(page, "05-v13-feedback.png");

  report.slow_consumer = await runSlowConsumerBackpressure(page);
  check(
    report,
    "slow_consumer_preserves_all_burst_tokens",
    report.slow_consumer,
  );

  const assistantCountBeforeFailure = await page
    .locator(".chat-message.assistant")
    .count();
  await terminateOwnedAgentProcess();
  const failurePrompt =
    "AI Server가 중단된 상태에서도 규칙 기반 대체 답변을 만들지 마세요.";
  const composer = page.getByRole("textbox", {
    name: "AI 에이전트에게 질문하기",
  });
  await composer.fill(failurePrompt);
  let failureStreamFinished = false;
  const failureRequestPromise = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      apiPath(request.url()) === "/api/ui/v1/chat/stream" &&
      requestBody(request)?.message === failurePrompt,
    { timeout: 20_000 },
  );
  const failureResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      apiPath(response.url()) === "/api/ui/v1/chat/stream" &&
      requestBody(response.request())?.message === failurePrompt,
    { timeout: 20_000 },
  );
  await page.getByRole("button", { name: "메시지 보내기" }).click();
  const failureRequest = await failureRequestPromise;
  const failureRequestPayload = requestBody(failureRequest);
  const failedOptimisticCard = page.locator(
    `.chat-message.user[data-message-id=${JSON.stringify(
      `client:${failureRequestPayload.request_id}`,
    )}]`,
  );
  await failedOptimisticCard.waitFor({ state: "visible" });
  assert(
    (await failedOptimisticCard.getAttribute("data-delivery-status")) ===
        "sending" &&
      failureStreamFinished === false,
    "FAILED_CHAT_OPTIMISTIC_MESSAGE_MISSING",
    "The failed request was not shown before its Backend terminal error.",
    { request: failureRequestPayload },
  );
  const failureResponse = await failureResponsePromise;
  const capturedFailure = await capturedChatStream(
    page,
    failureRequestPayload.request_id,
    20_000,
  );
  const failureEvents = parseSseEvents(capturedFailure.body);
  failureStreamFinished = true;
  const failureTerminals = failureEvents.filter((event) =>
    ["completed", "error"].includes(event.type),
  );
  assert(
    failureTerminals.length === 1 &&
      failureTerminals[0].type === "error" &&
      failureEvents.at(-1) === failureTerminals[0],
    "FAILED_CHAT_TERMINAL_INVALID",
    "An unavailable Agent must end with one explicit error event.",
    { failureEvents },
  );
  await failedOptimisticCard.waitFor({ state: "visible" });
  await pollUntil(
    () => failedOptimisticCard.getAttribute("data-delivery-status"),
    (status) => status === "failed",
    {
      timeoutMs: 10_000,
      code: "FAILED_CHAT_USER_STATE_MISSING",
      message: "The failed user message did not remain visible and retryable.",
    },
  );
  const runtimeError = page.locator(".chat-runtime-error[role=alert]");
  await runtimeError.waitFor({ state: "visible" });
  const runtimeErrorText = (await runtimeError.innerText()).trim();
  const assistantCountAfterFailure = await page
    .locator(".chat-message.assistant")
    .count();
  assert(
    assistantCountAfterFailure === assistantCountBeforeFailure &&
      runtimeErrorText.includes("대체 응답은 생성하지 않았습니다.") &&
      !runtimeErrorText.includes("요청을 확인했습니다"),
    "FABRICATED_ASSISTANT_RESPONSE_DETECTED",
    "The Backend fabricated an assistant response after the Agent became unavailable.",
    {
      assistantCountBeforeFailure,
      assistantCountAfterFailure,
      runtimeErrorText,
      failureEvents,
    },
  );
  report.no_fallback_failure = {
    request_id: failureRequestPayload.request_id,
    event_types: failureEvents.map((event) => event.type),
    error: failureTerminals[0].data,
    runtime_error_text: runtimeErrorText,
    assistant_count_before: assistantCountBeforeFailure,
    assistant_count_after: assistantCountAfterFailure,
  };
  check(report, "no_fabricated_response_on_agent_failure", report.no_fallback_failure);
  await screenshot(page, "06-v13-agent-failure.png");
}

async function main() {
  ensureDirectory(artifactDir);
  const report = {
    started_at: new Date().toISOString(),
    base_url: baseUrl,
    application_origin: applicationOrigin,
    artifact_dir: path.resolve(artifactDir),
    status: "running",
    checks: [],
    backend_state: {},
    structured_chat: {},
    failure: null,
  };
  const networkEntries = [];
  const browserErrors = [];
  const boundaryViolations = [];
  const responseReaders = [];
  const requestEntries = new Map();
  let browser;
  let page;

  try {
    assert(
      fs.existsSync(chromePath),
      "CHROME_NOT_FOUND",
      "Chrome executable was not found.",
      { chromePath },
    );
    browser = await chromium.launch({
      executablePath: chromePath,
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    });
    page = await browser.newPage({
      viewport: { width: 1440, height: 1100 },
      locale: "ko-KR",
      timezoneId: "Asia/Seoul",
    });
    page.setDefaultTimeout(20_000);
    await page.addInitScript(() => {
      const capturedStreams = [];
      Object.defineProperty(globalThis, "__codexCapturedChatStreams", {
        configurable: true,
        value: capturedStreams,
      });
      const originalFetch = globalThis.fetch.bind(globalThis);
      globalThis.fetch = async (...args) => {
        const [input, init] = args;
        const requestUrl =
          typeof input === "string" || input instanceof URL
            ? String(input)
            : input?.url;
        let requestPayload = null;
        if (typeof init?.body === "string") {
          try {
            requestPayload = JSON.parse(init.body);
          } catch {
            requestPayload = null;
          }
        }
        const response = await originalFetch(...args);
        try {
          const url = new URL(requestUrl, globalThis.location.href);
          const contentType =
            response.headers.get("content-type")?.toLowerCase() || "";
          if (
            url.pathname === "/api/ui/v1/chat/stream" &&
            contentType.includes("text/event-stream")
          ) {
            const entry = {
              request_id: requestPayload?.request_id ?? null,
              status: response.status,
              content_type: contentType,
              body: null,
              error: null,
              done: false,
            };
            capturedStreams.push(entry);
            response
              .clone()
              .text()
              .then((body) => {
                entry.body = body;
                entry.done = true;
              })
              .catch((error) => {
                entry.error = error?.message || String(error);
                entry.done = true;
              });
          }
        } catch {
          // The application still receives the original response. Capture
          // failures are surfaced by capturedChatStream with correlation data.
        }
        return response;
      };
    });

    page.on("console", (message) => {
      if (message.type() === "error") {
        browserErrors.push({
          source: "console",
          text: message.text(),
        });
      }
    });
    page.on("pageerror", (error) => {
      browserErrors.push({
        source: "pageerror",
        text: error.message,
      });
    });
    page.on("requestfailed", (request) => {
      const failedPayload = requestBody(request);
      if (
        failedPayload &&
        typeof failedPayload === "object" &&
        intentionallyAbortedRequestIds.has(failedPayload.request_id)
      ) {
        return;
      }
      browserErrors.push({
        source: "requestfailed",
        method: request.method(),
        url: request.url(),
        text: request.failure()?.errorText || "request_failed",
      });
    });
    page.on("request", (request) => {
      const requestUrl = new URL(request.url());
      const entry = {
        sequence: networkEntries.length + 1,
        method: request.method(),
        url: request.url(),
        path: requestUrl.pathname,
        resource_type: request.resourceType(),
        request_body: requestBody(request),
        response_status: null,
        response_body: null,
      };
      networkEntries.push(entry);
      requestEntries.set(request, entry);

      if (!["http:", "https:"].includes(requestUrl.protocol)) {
        return;
      }
      if (requestUrl.origin !== applicationOrigin) {
        boundaryViolations.push({
          method: request.method(),
          url: request.url(),
          reason: "cross_origin_browser_request",
        });
      }
      if (
        requestUrl.pathname.startsWith("/api/") &&
        !requestUrl.pathname.startsWith("/api/ui/v1/")
      ) {
        boundaryViolations.push({
          method: request.method(),
          url: request.url(),
          reason: "non_bff_api_request",
        });
      }
    });
    page.on("response", (response) => {
      const entry = requestEntries.get(response.request());
      if (!entry) {
        return;
      }
      entry.response_status = response.status();
      if (!entry.path.startsWith("/api/ui/v1/")) {
        return;
      }
      if (
        (response.headers()["content-type"] || "")
          .toLowerCase()
          .includes("text/event-stream")
      ) {
        entry.response_body = {
          capture: "browser_fetch_clone",
        };
        return;
      }
      const reader = response
        .text()
        .then((body) => {
          entry.response_body = safeJsonParse(body) ?? body;
        })
        .catch((error) => {
          entry.response_body = {
            capture_error: error.message,
          };
        });
      responseReaders.push(reader);
    });

    const navigation = await page.goto(`${baseUrl}/`, {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
    assert(
      navigation?.status() === 200,
      "REACT_ROOT_NAVIGATION_FAILED",
      "The application root did not return HTTP 200.",
      { status: navigation?.status() ?? null },
    );
    await page.locator("#root").waitFor({ state: "visible" });
    await page
      .getByRole("heading", {
        name: "닥터앤서 AI 복약 케어 테스트",
        level: 1,
      })
      .waitFor({ state: "visible" });
    await page
      .getByRole("tab", { name: "HOME", exact: true })
      .waitFor({ state: "visible" });
    await page
      .getByRole("tab", { name: "Chat", exact: true })
      .waitFor({ state: "visible" });
    check(report, "react_root", {
      status: navigation.status(),
      title: await page.title(),
    });
    await screenshot(page, "01-react-root.png");

    const statusResult = await page.evaluate(async () => {
      const response = await fetch("/api/ui/v1/status", {
        headers: { Accept: "application/json" },
      });
      return {
        status: response.status,
        payload: await response.json(),
      };
    });
    const statusData = publicEnvelopeData(statusResult.payload);
    assert(
      statusResult.status === 200 && statusData,
      "STATUS_CONTRACT_FAILED",
      "The BFF status endpoint did not return a successful public envelope.",
      statusResult,
    );
    for (const [label, key] of [
      ["Backend", "backend_server"],
      ["AI Server", "ai_server"],
    ]) {
      const service = statusData[key];
      const indicator = page.locator(".service-state").filter({
        hasText: label,
      });
      await indicator.waitFor({ state: "visible" });
      const uiStatus = await indicator.getAttribute("data-status");
      assert(
        uiStatus === service.status,
        "STATUS_UI_MISMATCH",
        `${label} UI status does not match the BFF probe result.`,
        { label, uiStatus, service },
      );
      const uiText = (await indicator.innerText()).trim();
      assert(
        (uiText.endsWith("정상") && service.status === "ready") ||
          (!uiText.endsWith("정상") && service.status !== "ready"),
        "STATUS_FALSE_HEALTHY",
        `${label} displayed a healthy label without a ready probe result.`,
        { label, uiText, service },
      );
    }
    assert(
      statusData.backend_server.status === "ready",
      "BACKEND_NOT_READY",
      "The real-service flow requires a ready Backend probe.",
      statusData.backend_server,
    );
    assert(
      statusData.ai_server.status === "ready",
      "AI_SERVER_NOT_READY",
      "The real-service flow requires an actual generation-provider-ready AI Server.",
      statusData.ai_server,
    );
    assert(
      statusData.backend_server.evidence.includes("DATABASE_PROBE_OK"),
      "BACKEND_READY_EVIDENCE_MISSING",
      "Backend readiness did not include a successful database probe.",
      statusData.backend_server,
    );
    assert(
      statusData.ai_server.evidence.includes("GENERATION_PROVIDER_OK"),
      "AI_READY_EVIDENCE_MISSING",
      "AI readiness did not include a successful generation provider probe.",
      statusData.ai_server,
    );
    report.service_status = statusData;
    check(report, "actual_service_readiness", statusData);

    page.once("dialog", (dialog) => dialog.accept());
    const resetResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        apiPath(response.url()) === "/api/ui/v1/testbed/reset",
    );
    await page.getByRole("button", { name: "전체 리셋" }).click();
    const resetResponse = await resetResponsePromise;
    assert(
      resetResponse.status() === 200,
      "TESTBED_RESET_FAILED",
      "The isolated testbed could not be reset.",
      { status: resetResponse.status() },
    );
    await page
      .getByRole("button", { name: "테스트 복약 일정 설정" })
      .waitFor({ state: "visible" });

    const stopButton = page.getByRole("button", {
      name: "정지",
      exact: true,
    });
    if (await stopButton.isEnabled()) {
      const pauseResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          apiPath(response.url()) === "/api/ui/v1/clock/pause",
      );
      await stopButton.click();
      const pauseResponse = await pauseResponsePromise;
      assert(
        pauseResponse.status() === 200,
        "CLOCK_PAUSE_FAILED",
        "The deterministic test clock could not be paused.",
        { status: pauseResponse.status() },
      );
    }

    await page
      .getByRole("button", { name: "테스트 복약 일정 설정" })
      .click();
    const scheduleDialog = page.getByRole("dialog", {
      name: "테스트 복약 일정 설정",
    });
    await scheduleDialog.waitFor({ state: "visible" });
    await scheduleDialog
      .getByRole("radio", { name: /복합 복약/ })
      .check();
    const scenarioResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        apiPath(response.url()) ===
          "/api/ui/v1/medication-scenarios/apply",
    );
    await scheduleDialog
      .getByRole("button", { name: "일정 적용", exact: true })
      .click();
    const scenarioResponse = await scenarioResponsePromise;
    const scenarioPayload = await scenarioResponse.json();
    const scenarioData = publicEnvelopeData(scenarioPayload);
    assert(
      scenarioResponse.status() === 200 && scenarioData,
      "SCENARIO_APPLY_FAILED",
      "The combined medication scenario was not applied.",
      {
        status: scenarioResponse.status(),
        payload: scenarioPayload,
      },
    );
    assert(
      scenarioData.medications?.length === 4,
      "SCENARIO_MEDICATION_COUNT_MISMATCH",
      "The combined scenario must expose four public medication schedule items.",
      { medications: scenarioData.medications },
    );
    await page
      .locator("#today-medication-list .medication-item")
      .filter({ hasText: "메트포르민" })
      .waitFor({ state: "visible" });
    await page
      .locator("#today-medication-list .medication-item")
      .filter({ hasText: "암로디핀" })
      .waitFor({ state: "visible" });
    await page
      .locator("#today-medication-list .medication-item")
      .filter({ hasText: "수니티닙" })
      .waitFor({ state: "visible" });
    await page
      .locator("#today-medication-list .medication-item")
      .filter({ hasText: "레트로졸" })
      .waitFor({ state: "visible" });
    check(report, "scenario_and_medications", {
      scenario: scenarioData.scenario,
      medication_count: scenarioData.medications.length,
      medication_names: scenarioData.medications.map(
        (item) => item.medication_name,
      ),
    });
    await screenshot(page, "02-combined-scenario.png");

    const missedPolicyResult = await page.evaluate(
      async (enabled) => {
        const response = await fetch(
          "/api/ui/v1/policies/missed_dose_conversation",
          {
            method: "PUT",
            headers: {
              Accept: "application/json",
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ enabled }),
          },
        );
        return {
          status: response.status,
          payload: await response.json(),
        };
      },
      deterministicV13Mode,
    );
    assert(
      missedPolicyResult.status === 200 &&
        publicEnvelopeData(missedPolicyResult.payload),
      "MISSED_POLICY_UPDATE_FAILED",
      "The isolated test could not configure the missed-dose chat policy.",
      missedPolicyResult,
    );

    const advanceResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        apiPath(response.url()) === "/api/ui/v1/clock/advance",
    );
    await page.getByRole("button", { name: "3시간 진행" }).click();
    const advanceResponse = await advanceResponsePromise;
    assert(
      advanceResponse.status() === 200,
      "CLOCK_ADVANCE_FAILED",
      "The simulation clock could not be advanced to a missed-dose state.",
      { status: advanceResponse.status() },
    );
    const missedTimelineItem = page.locator(
      "#medication-timeline .timeline-item.status-missed",
    );
    await missedTimelineItem.first().waitFor({ state: "visible" });
    const missedSidebarItems = page.locator(
      "#today-medication-list .medication-item.is-missed",
    );
    const missedSidebarCount = await missedSidebarItems.count();
    assert(
      missedSidebarCount > 0,
      "MISSED_STATUS_NOT_RENDERED",
      "A Backend missed dose was not rendered as missed in the HOME medication list.",
    );
    const missedTexts = await missedSidebarItems.allInnerTexts();
    assert(
      missedTexts.every((text) => text.includes("미복용")),
      "MISSED_STATUS_LABEL_MISMATCH",
      "Missed medication cards must display the 미복용 label.",
      { missedTexts },
    );
    check(report, "missed_status_ui", {
      sidebar_count: missedSidebarCount,
      timeline_count: await missedTimelineItem.count(),
      labels: missedTexts,
    });
    await screenshot(page, "03-missed-status.png");

    if (deterministicV13Mode) {
      await runDeterministicV13Core(page, report);
      await Promise.allSettled(responseReaders);
      const apiNetworkEntries = networkEntries.filter((entry) =>
        entry.path.startsWith("/api/ui/v1/"),
      );
      const payloadViolations = [];
      for (const entry of apiNetworkEntries) {
        collectPayloadViolations(
          entry.request_body,
          `${entry.method} ${entry.path} request`,
          [],
          payloadViolations,
        );
        collectPayloadViolations(
          entry.response_body,
          `${entry.method} ${entry.path} response`,
          [],
          payloadViolations,
        );
      }
      report.browser_boundary = {
        boundary_violations: boundaryViolations,
        payload_violations: payloadViolations,
      };
      assert(
        boundaryViolations.length === 0,
        "BROWSER_BFF_BOUNDARY_VIOLATION",
        "The browser called an origin or API path outside the Backend BFF.",
        { boundaryViolations },
      );
      assert(
        payloadViolations.length === 0,
        "BROWSER_PRIVATE_PAYLOAD_EXPOSURE",
        "A browser request or response exposed private Backend identifiers.",
        { payloadViolations },
      );
      assert(
        browserErrors.length === 0,
        "BROWSER_RUNTIME_ERRORS",
        "Browser console, page, or transport errors were observed.",
        { browserErrors },
      );
      check(report, "browser_bff_only", {
        api_request_count: apiNetworkEntries.length,
        origin: applicationOrigin,
      });
      check(report, "browser_payload_trust_boundary", {
        payload_violations: 0,
      });
      check(report, "browser_runtime_errors", { count: 0 });
      report.status = "passed";
      report.finished_at = new Date().toISOString();
      writeJson("network.json", networkEntries);
      writeJson("summary.json", report);
      console.log(
        `PASS deterministic v1.3 real-service validation: ${path.resolve(
          artifactDir,
        )}`,
      );
      return;
    }

    const initialBackendState = await backendNutritionEvidence(page);
    report.backend_state.before_meal_request = initialBackendState;

    await page.getByRole("tab", { name: "Chat", exact: true }).click();
    const composer = page.getByRole("textbox", {
      name: "AI 에이전트에게 질문하기",
    });
    await composer.waitFor({ state: "visible" });
    await composer.fill(mealPrompt);
    const initialChatResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        apiPath(response.url()) === "/api/ui/v1/chat/sync" &&
        !isStructuredChatRequest(response.request()),
      { timeout: llmTimeoutMs },
    );
    await page.getByRole("button", { name: "메시지 보내기" }).click();
    const initialChatResponse = await initialChatResponsePromise;
    const initialChatPayload = await initialChatResponse.json();
    const initialChatData = publicEnvelopeData(initialChatPayload);
    assert(
      initialChatResponse.status() === 200 && initialChatData,
      "MEAL_CONFIRMATION_REQUEST_FAILED",
      "The real LLM meal request did not complete successfully.",
      {
        status: initialChatResponse.status(),
        payload: initialChatPayload,
      },
    );

    const initialInteractionDetails = {
      request_id: initialChatData.request_id,
      assistant_message_id: initialChatData.assistant_message_id,
      message_type: initialChatData.message_type,
      message_title: initialChatData.message?.message_title ?? null,
      message_text: initialChatData.message?.text ?? null,
      selections: initialChatData.message?.selections ?? [],
      inputs: initialChatData.message?.inputs ?? null,
    };
    report.structured_chat.initial_interaction =
      initialInteractionDetails;
    const confirmationData = await advanceMealInteractionToApproval(
      page,
      initialChatData,
      initialBackendState,
      report,
    );
    const confirmationSelections =
      confirmationData.message?.selections ?? [];
    const confirmationDetails = {
      request_id: confirmationData.request_id,
      assistant_message_id: confirmationData.assistant_message_id,
      message_type: confirmationData.message_type,
      message_title: confirmationData.message?.message_title ?? null,
      message_text: confirmationData.message?.text ?? null,
      selections: confirmationSelections,
    };
    report.structured_chat.confirmation = confirmationDetails;

    const confirmationCard = page.locator(
      `.chat-message.assistant[data-message-id=${JSON.stringify(
        confirmationData.assistant_message_id,
      )}]`,
    );
    await confirmationCard.waitFor({ state: "visible" });
    const renderedMessageType =
      await confirmationCard.getAttribute("data-message-type");
    assert(
      renderedMessageType === "selection_box",
      "MEAL_CONFIRMATION_UI_TYPE_MISMATCH",
      "The Backend confirmation response was not rendered as a selection box.",
      {
        assistantMessageId: confirmationData.assistant_message_id,
        renderedMessageType,
      },
    );
    const confirmationText = await confirmationCard.innerText();
    const confirmationPhraseViolations =
      assistantPhraseViolations(confirmationText);
    assert(
      confirmationPhraseViolations.length === 0,
      "FORBIDDEN_ASSISTANT_PHRASE",
      "The confirmation answer contained a PHR-missing or fallback phrase.",
      {
        assistantMessageId: confirmationData.assistant_message_id,
        confirmationPhraseViolations,
        confirmationText,
      },
    );

    const beforeConfirmationState =
      await backendNutritionEvidence(page);
    report.backend_state.before_explicit_confirmation =
      beforeConfirmationState;
    assert(
      beforeConfirmationState.meal_count ===
        initialBackendState.meal_count,
      "MEAL_SAVED_BEFORE_CONFIRMATION",
      "A nutrition meal row was created before the user selected 기록.",
      {
        initial: initialBackendState,
        beforeConfirmation: beforeConfirmationState,
      },
    );
    check(report, "meal_confirmation_boundary_before_write", {
      meal_count: beforeConfirmationState.meal_count,
      selections: confirmationSelections,
    });
    await screenshot(page, "04-meal-confirmation-before-write.png");

    const structuredResponsePromise = page.waitForResponse(
      (response) => isStructuredChatRequest(response.request()),
      { timeout: llmTimeoutMs },
    );
    await confirmationCard
      .getByRole("button", { name: "기록", exact: true })
      .click();
    const structuredResponse = await structuredResponsePromise;
    const structuredRequest = requestBody(structuredResponse.request());
    const structuredPayload = await structuredResponse.json();
    const structuredData = publicEnvelopeData(structuredPayload);
    report.structured_chat.record_request = structuredRequest;
    report.structured_chat.record_response = {
      status: structuredResponse.status(),
      data: structuredData,
      error: structuredPayload?.error ?? null,
    };
    assert(
      structuredResponse.status() === 200 && structuredData,
      "MEAL_CONFIRMATION_APPLY_FAILED",
      "The explicit 기록 selection did not complete successfully.",
      {
        status: structuredResponse.status(),
        request: structuredRequest,
        payload: structuredPayload,
      },
    );
    assert(
      structuredRequest?.source_message_id ===
        confirmationData.assistant_message_id,
      "STRUCTURED_SOURCE_MESSAGE_ID_MISMATCH",
      "The structured response was not bound to the exact confirmation assistant message.",
      {
        expected: confirmationData.assistant_message_id,
        actual: structuredRequest?.source_message_id ?? null,
        request: structuredRequest,
      },
    );
    assert(
      structuredRequest?.requested_return_type === "selection_box" &&
        structuredRequest?.message === "기록" &&
        typeof structuredRequest?.request_id === "string" &&
        structuredRequest.request_id.length > 0,
      "STRUCTURED_REQUEST_CONTRACT_MISMATCH",
      "The explicit confirmation request did not use the structured chat contract.",
      { request: structuredRequest },
    );
    check(report, "exact_structured_source_message_id", {
      source_message_id: structuredRequest.source_message_id,
      requested_return_type: structuredRequest.requested_return_type,
      request_id: structuredRequest.request_id,
    });

    const afterConfirmationState = await pollUntil(
      () => backendNutritionEvidence(page),
      (value) =>
        value.meal_count === initialBackendState.meal_count + 1,
      {
        timeoutMs: 15_000,
        code: "MEAL_WRITE_COUNT_MISMATCH",
        message:
          "The explicit 기록 selection did not create exactly one meal row.",
      },
    );
    report.backend_state.after_explicit_confirmation =
      afterConfirmationState;
    assert(
      afterConfirmationState.meal_count ===
        initialBackendState.meal_count + 1,
      "MEAL_WRITE_COUNT_MISMATCH",
      "The explicit 기록 selection must create exactly one meal row.",
      {
        initial: initialBackendState,
        afterConfirmation: afterConfirmationState,
      },
    );

    const finalAssistantCard = page.locator(
      `.chat-message.assistant[data-message-id=${JSON.stringify(
        structuredData.assistant_message_id,
      )}]`,
    );
    await finalAssistantCard.waitFor({ state: "visible" });
    const finalAssistantText = await finalAssistantCard.innerText();
    const finalPhraseViolations =
      assistantPhraseViolations(finalAssistantText);
    assert(
      finalPhraseViolations.length === 0,
      "FORBIDDEN_ASSISTANT_PHRASE",
      "The post-confirmation answer contained a PHR-missing or fallback phrase.",
      {
        assistantMessageId: structuredData.assistant_message_id,
        finalPhraseViolations,
        finalAssistantText,
      },
    );
    report.structured_chat.assistant_texts = {
      confirmation: confirmationText,
      final: finalAssistantText,
    };
    check(report, "meal_write_after_explicit_confirmation", {
      before: initialBackendState.meal_count,
      after: afterConfirmationState.meal_count,
      final_message_type: structuredData.message_type,
    });

    const replayResult = await page.evaluate(async (body) => {
      const response = await fetch("/api/ui/v1/chat/sync", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      });
      return {
        status: response.status,
        payload: await response.json(),
      };
    }, structuredRequest);
    const replayData = publicEnvelopeData(replayResult.payload);
    assert(
      replayResult.status === 200 && replayData,
      "MEAL_CONFIRMATION_REPLAY_FAILED",
      "The identical structured request was not replayed successfully.",
      replayResult,
    );
    assert(
      replayData.request_id === structuredData.request_id &&
        replayData.assistant_message_id ===
          structuredData.assistant_message_id,
      "MEAL_CONFIRMATION_REPLAY_RESULT_CHANGED",
      "The idempotent replay did not return the original chat result.",
      {
        original: {
          request_id: structuredData.request_id,
          assistant_message_id: structuredData.assistant_message_id,
        },
        replay: {
          request_id: replayData.request_id,
          assistant_message_id: replayData.assistant_message_id,
        },
      },
    );
    const afterReplayState = await backendNutritionEvidence(page);
    report.backend_state.after_identical_replay =
      afterReplayState;
    assert(
      afterReplayState.meal_count ===
        initialBackendState.meal_count + 1,
      "MEAL_CONFIRMATION_REPLAY_DUPLICATED_WRITE",
      "Replaying the same structured request created another meal row.",
      {
        initial: initialBackendState,
        afterConfirmation: afterConfirmationState,
        afterReplay: afterReplayState,
      },
    );
    check(report, "meal_confirmation_idempotent_replay", {
      request_id: structuredRequest.request_id,
      meal_count: afterReplayState.meal_count,
    });

    await page.getByRole("tab", { name: "HOME", exact: true }).click();
    await pollUntil(
      async () => {
        const text = await page
          .locator(".nutrition-panel .panel-subtitle")
          .innerText();
        return text;
      },
      (text) => text.includes(
        `식사 ${afterReplayState.meal_count}건 기록`,
      ),
      {
        timeoutMs: 15_000,
        code: "MEAL_DASHBOARD_NOT_RECONCILED",
        message:
          "The HOME nutrition summary did not reconcile after the confirmed write.",
      },
    );

    await Promise.allSettled(responseReaders);
    const apiNetworkEntries = networkEntries.filter((entry) =>
      entry.path.startsWith("/api/ui/v1/"),
    );
    const payloadViolations = [];
    for (const entry of apiNetworkEntries) {
      collectPayloadViolations(
        entry.request_body,
        `${entry.method} ${entry.path} request`,
        [],
        payloadViolations,
      );
      collectPayloadViolations(
        entry.response_body,
        `${entry.method} ${entry.path} response`,
        [],
        payloadViolations,
      );
    }
    report.browser_boundary = {
      boundary_violations: boundaryViolations,
      payload_violations: payloadViolations,
    };
    assert(
      boundaryViolations.length === 0,
      "BROWSER_BFF_BOUNDARY_VIOLATION",
      "The browser called an origin or API path outside the Backend BFF.",
      { boundaryViolations },
    );
    assert(
      payloadViolations.length === 0,
      "BROWSER_PRIVATE_PAYLOAD_EXPOSURE",
      "A browser request or response exposed patient_id, an internal numeric identifier, or version.",
      { payloadViolations },
    );
    check(report, "browser_bff_only", {
      api_request_count: apiNetworkEntries.length,
      origin: applicationOrigin,
    });
    check(report, "browser_payload_trust_boundary", {
      payload_violations: 0,
    });

    assert(
      browserErrors.length === 0,
      "BROWSER_RUNTIME_ERRORS",
      "Browser console, page, or request errors were observed.",
      { browserErrors },
    );
    check(report, "browser_runtime_errors", { count: 0 });

    await page.getByRole("tab", { name: "Chat", exact: true }).click();
    await screenshot(page, "05-meal-recorded-and-replayed.png");
    report.status = "passed";
    report.finished_at = new Date().toISOString();
    writeJson("network.json", networkEntries);
    writeJson("summary.json", report);
    console.log(
      `PASS real-service P0 validation: ${path.resolve(artifactDir)}`,
    );
  } catch (error) {
    report.status = "failed";
    report.finished_at = new Date().toISOString();
    report.failure = {
      code: error.code || "UNEXPECTED_ERROR",
      message: error.message,
      diagnostic: error.diagnostic || null,
      stack: error.stack || null,
    };
    await Promise.allSettled(responseReaders);
    if (page) {
      await screenshot(page, "failure.png").catch(() => {});
      report.failure.page_url = page.url();
      report.failure.visible_text = await page
        .locator("body")
        .innerText()
        .catch(() => "");
    }
    writeJson("network.json", networkEntries);
    writeJson("browser-errors.json", browserErrors);
    writeJson("summary.json", report);
    throw error;
  } finally {
    await browser?.close().catch(() => {});
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

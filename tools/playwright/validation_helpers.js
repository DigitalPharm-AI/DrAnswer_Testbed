"use strict";

const forbiddenAssistantPhrases = [
  /PHR\s*(?:환자\s*정보)?(?:가\s*)?(?:미등록|등록되지|찾지\s*못)/iu,
  /phr_patient_key/iu,
  /말씀을\s*확인했습니다\.\s*복약이나\s*증상과\s*관련해\s*더\s*이야기해\s*주세요/iu,
  /완료\s*전\s*임시\s*응답/iu,
  /질문이\s*준비되고\s*있습니다/iu,
];
function safeJsonParse(value) {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function check(report, name, details = {}) {
  report.checks.push({
    name,
    status: "passed",
    at: new Date().toISOString(),
    details,
  });
}

function diagnosticError(code, message, details = {}) {
  const error = new Error(`${code}: ${message}`);
  error.code = code;
  error.diagnostic = details;
  return error;
}

function assert(condition, code, message, details = {}) {
  if (!condition) {
    throw diagnosticError(code, message, details);
  }
}

function publicEnvelopeData(payload) {
  if (
    payload &&
    typeof payload === "object" &&
    payload.success === true &&
    payload.data &&
    typeof payload.data === "object"
  ) {
    return payload.data;
  }
  return null;
}

async function backendNutritionEvidence(page) {
  const result = await page.evaluate(async () => {
    const response = await fetch("/api/ui/v1/nutrition", {
      method: "GET",
      headers: {
        Accept: "application/json",
      },
      cache: "no-store",
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    return {
      status: response.status,
      payload,
    };
  });
  const data = publicEnvelopeData(result.payload);
  assert(
    result.status === 200 && data,
    "BACKEND_STATE_ORACLE_FAILED",
    "The Backend nutrition state could not be read through the testbed API.",
    result,
  );
  const meals = Array.isArray(data.meals) ? data.meals : [];
  const mealCount = Number(data.summary?.total_meals);
  const foodCount = meals.reduce(
    (total, meal) =>
      total + (Array.isArray(meal?.foods) ? meal.foods.length : 0),
    0,
  );
  assert(
    Number.isInteger(mealCount) && mealCount >= 0,
    "BACKEND_STATE_ORACLE_INVALID",
    "The Backend nutrition API returned an invalid meal count.",
    { data },
  );
  return {
    meal_count: mealCount,
    food_count: foodCount,
  };
}

function collectPayloadViolations(value, location, trail = [], results = []) {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      collectPayloadViolations(item, location, [...trail, index], results),
    );
    return results;
  }
  if (!value || typeof value !== "object") {
    return results;
  }

  for (const [key, child] of Object.entries(value)) {
    const childTrail = [...trail, key];
    const pathLabel = childTrail.join(".");
    if (key === "patient_id") {
      results.push({
        location,
        path: pathLabel,
        reason: "patient_id_exposed",
      });
    }
    if (key === "version") {
      results.push({
        location,
        path: pathLabel,
        reason: "internal_version_exposed",
      });
    }
    if (
      (key === "id" || key.endsWith("_id")) &&
      typeof child === "number"
    ) {
      results.push({
        location,
        path: pathLabel,
        reason: "internal_numeric_id_exposed",
      });
    }
    collectPayloadViolations(child, location, childTrail, results);
  }
  return results;
}

function apiPath(url) {
  try {
    return new URL(url).pathname;
  } catch {
    return "";
  }
}

function requestBody(request) {
  const text = request.postData();
  return safeJsonParse(text) ?? text ?? null;
}

function isStructuredChatRequest(request) {
  if (
    request.method() !== "POST" ||
    apiPath(request.url()) !== "/api/ui/v1/chat/sync"
  ) {
    return false;
  }
  const body = requestBody(request);
  return (
    body &&
    typeof body === "object" &&
    (body.requested_return_type === "selection_box" ||
      body.requested_return_type === "input_box")
  );
}

function assistantPhraseViolations(text) {
  return forbiddenAssistantPhrases
    .filter((pattern) => pattern.test(text))
    .map((pattern) => pattern.source);
}

function isMealApprovalCard(data) {
  const selections = data?.message?.selections ?? [];
  return (
    data?.message_type === "selection_box" &&
    selections.includes("기록") &&
    selections.includes("취소")
  );
}
function parseSseEvents(rawBody) {
  return rawBody
    .replace(/\r\n/g, "\n")
    .split("\n\n")
    .map((block) => block.trim())
    .filter(Boolean)
    .filter((block) => !block.startsWith(":"))
    .map((block) => {
      let type = "message";
      const dataLines = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) {
          type = line.slice("event:".length).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice("data:".length).trimStart());
        }
      }
      const rawData = dataLines.join("\n");
      const data = safeJsonParse(rawData);
      assert(
        data !== null,
        "UI_SSE_EVENT_INVALID",
        "The Backend UI stream emitted a non-JSON SSE event.",
        { type, rawData },
      );
      return { type, data };
    });
}
function assertCompletedSseContract(events, requestId) {
  assert(
    events[0]?.type === "start" &&
      events[0]?.data?.request_id === requestId,
    "UI_SSE_START_INVALID",
    "The Backend UI stream did not begin with the correlated start event.",
    { requestId, events },
  );
  const deltas = events.filter((event) => event.type === "text_delta");
  deltas.forEach((event, index) => {
    assert(
      event.data?.request_id === requestId &&
        event.data?.sequence === index &&
        typeof event.data?.text === "string" &&
        event.data.text.length > 0,
      "UI_SSE_DELTA_INVALID",
      "The Backend UI text deltas were not contiguous and correlated.",
      { requestId, index, event },
    );
  });
  const terminals = events.filter((event) =>
    ["completed", "error"].includes(event.type),
  );
  assert(
    terminals.length === 1 &&
      terminals[0].type === "completed" &&
      events.at(-1) === terminals[0],
    "UI_SSE_TERMINAL_INVALID",
    "The successful Backend UI stream must end in exactly one completed event.",
    { requestId, events },
  );
  const completed = terminals[0].data;
  assert(
    completed?.request_id === requestId &&
      typeof completed?.user_message_id === "string" &&
      typeof completed?.assistant_message_id === "string",
    "UI_SSE_COMPLETED_CORRELATION_INVALID",
    "The completed event did not contain correlated public message IDs.",
    { requestId, completed },
  );
  return { completed, deltas };
}
module.exports = {
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
};

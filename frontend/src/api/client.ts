import type {
  ApiEnvelope,
  ChatHistoryData,
  ChatStreamEvent,
  ChatSyncData,
  ChatSyncRequest,
  DashboardData,
  FeedbackData,
  FeedbackRequest,
  MedicationDose,
  PolicyKey,
  ScenarioApplyData,
  ScenarioListData,
  SimulationClock,
  SimulationSpeed,
  SystemStatusData,
  TestbedResetData,
  UiPolicies,
} from "./contracts";
import { newRequestId } from "../utils/publicIds";

const API_ROOT = "/api/ui/v1";
const DEFAULT_REQUEST_TIMEOUT_MS = 20_000;
const READINESS_REQUEST_TIMEOUT_MS = 12_000;
const CHAT_REQUEST_TIMEOUT_MS = 120_000;
const MAX_CHAT_STREAM_FRAME_BYTES = 1_048_576;

type UiRequestInit = RequestInit & {
  timeoutMs?: number;
};

export class UiApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown> | null;
  readonly retryable: boolean;
  readonly status: number;

  constructor(
    message: string,
    options: {
      code?: string;
      details?: Record<string, unknown> | null;
      retryable: boolean;
      status: number;
    },
  ) {
    super(message);
    this.name = "UiApiError";
    this.code = options.code ?? "HTTP_ERROR";
    this.details = options.details ?? null;
    this.retryable = options.retryable;
    this.status = options.status;
  }
}

function isRetryableHttpStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isApiEnvelope<T>(payload: unknown): payload is ApiEnvelope<T> {
  if (!isRecord(payload) || typeof payload.success !== "boolean") {
    return false;
  }
  if (payload.success) {
    return (
      "data" in payload &&
      payload.data !== null &&
      payload.data !== undefined &&
      payload.error === null
    );
  }
  if (payload.data !== null || !isRecord(payload.error)) {
    return false;
  }
  const details = payload.error.details;
  return (
    typeof payload.error.code === "string" &&
    typeof payload.error.message === "string" &&
    typeof payload.error.retryable === "boolean" &&
    "details" in payload.error &&
    (details === null || isRecord(details))
  );
}

async function request<T>(path: string, init?: UiRequestInit): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = init?.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  const { timeoutMs: _timeoutMs, ...requestInit } = init ?? {};
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      ...requestInit,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(requestInit.body
          ? { "Content-Type": "application/json" }
          : {}),
        ...requestInit.headers,
      },
    });
  } catch {
    if (controller.signal.aborted) {
      throw new UiApiError("서버 응답 시간이 초과되었습니다.", {
        code: "REQUEST_TIMEOUT",
        retryable: true,
        status: 0,
      });
    }
    throw new UiApiError("Backend 서버에 연결할 수 없습니다.", {
      code: "NETWORK_ERROR",
      retryable: true,
      status: 0,
    });
  } finally {
    window.clearTimeout(timeoutId);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new UiApiError("서버 응답을 확인할 수 없습니다.", {
      code: "INVALID_RESPONSE",
      retryable: false,
      status: response.status,
    });
  }

  if (isApiEnvelope<T>(payload)) {
    const envelope = payload;
    if (response.ok && envelope.success) {
      return envelope.data;
    }
    if (!envelope.success) {
      throw new UiApiError(envelope.error.message, {
        code: envelope.error.code,
        details: envelope.error.details,
        retryable: envelope.error.retryable,
        status: response.status,
      });
    }
  }
  if (
    typeof payload === "object" &&
    payload !== null &&
    "detail" in payload &&
    typeof payload.detail === "object" &&
    payload.detail !== null
  ) {
    const detail = payload.detail as {
      code?: string;
      message?: string;
      retryable?: boolean;
      details?: Record<string, unknown> | null;
    };
    if (detail.message) {
      throw new UiApiError(detail.message, {
        code: detail.code,
        details: detail.details,
        retryable:
          detail.retryable ?? isRetryableHttpStatus(response.status),
        status: response.status,
      });
    }
  }

  throw new UiApiError("서버 응답을 확인할 수 없습니다.", {
    code: response.ok ? "INVALID_RESPONSE" : "HTTP_ERROR",
    retryable: response.ok
      ? false
      : isRetryableHttpStatus(response.status),
    status: response.status,
  });
}

async function streamChat(
  payload: ChatSyncRequest,
  onEvent: (event: ChatStreamEvent) => void,
): Promise<ChatSyncData> {
  const requestId = payload.request_id ?? newRequestId();
  const streamPayload: ChatSyncRequest = {
    ...payload,
    request_id: requestId,
  };
  const controller = new AbortController();
  const timeoutId = window.setTimeout(
    () => controller.abort(),
    CHAT_REQUEST_TIMEOUT_MS,
  );
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}/chat/stream`, {
      method: "POST",
      signal: controller.signal,
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(streamPayload),
    });
  } catch {
    window.clearTimeout(timeoutId);
    if (controller.signal.aborted) {
      throw new UiApiError("서버 응답 시간이 초과되었습니다.", {
        code: "REQUEST_TIMEOUT",
        retryable: true,
        status: 0,
      });
    }
    throw new UiApiError("Backend 서버에 연결할 수 없습니다.", {
      code: "NETWORK_ERROR",
      retryable: true,
      status: 0,
    });
  }

  if (!response.ok || !response.body) {
    window.clearTimeout(timeoutId);
    throw await streamHttpError(response);
  }
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  if (!contentType.includes("text/event-stream")) {
    window.clearTimeout(timeoutId);
    throw new UiApiError("Backend 스트림 형식을 확인할 수 없습니다.", {
      code: "INVALID_STREAM_RESPONSE",
      retryable: false,
      status: response.status,
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let completed: ChatSyncData | null = null;
  let expectedSequence = 0;
  let started = false;
  let terminated = false;

  const consumeFrame = (frame: string) => {
    if (
      new TextEncoder().encode(frame).byteLength >
      MAX_CHAT_STREAM_FRAME_BYTES
    ) {
      throw invalidChatStreamError(
        "AI 응답 스트림 이벤트가 허용 크기를 초과했습니다.",
        response.status,
      );
    }
    const event = parseChatStreamFrame(frame);
    if (!event) {
      return;
    }
    if (terminated) {
      throw invalidChatStreamError(
        "완료 이후에 추가 AI 응답 이벤트가 도착했습니다.",
        response.status,
      );
    }
    if (event.type === "start") {
      if (started || event.request_id !== requestId) {
        throw invalidChatStreamError(
          "AI 응답 스트림 시작 식별자가 올바르지 않습니다.",
          response.status,
        );
      }
      started = true;
    } else if (event.type === "text_delta") {
      if (
        !started ||
        event.request_id !== requestId ||
        event.sequence !== expectedSequence ||
        !event.text
      ) {
        throw invalidChatStreamError(
          "AI 응답 스트림 순서가 올바르지 않습니다.",
          response.status,
        );
      }
      expectedSequence += 1;
    } else if (event.type === "completed") {
      if (!started || event.data.request_id !== requestId) {
        throw invalidChatStreamError(
          "AI 완료 응답 식별자가 요청과 일치하지 않습니다.",
          response.status,
        );
      }
      completed = event.data;
      terminated = true;
    } else {
      if (!started || event.request_id !== requestId) {
        throw invalidChatStreamError(
          "AI 오류 이벤트 식별자가 요청과 일치하지 않습니다.",
          response.status,
        );
      }
      terminated = true;
    }
    onEvent(event);
    if (event.type === "error") {
      throw new UiApiError(event.error.message, {
        code: event.error.code,
        details: event.error.details,
        retryable: event.error.retryable,
        status: response.status,
      });
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.replace(/\r\n/g, "\n").split("\n\n");
      buffer = frames.pop() ?? "";
      if (
        new TextEncoder().encode(buffer).byteLength >
        MAX_CHAT_STREAM_FRAME_BYTES
      ) {
        throw invalidChatStreamError(
          "AI 응답 스트림 버퍼가 허용 크기를 초과했습니다.",
          response.status,
        );
      }
      for (const frame of frames) {
        consumeFrame(frame);
      }
      if (done) {
        if (buffer.trim()) {
          consumeFrame(buffer.replace(/\r\n/g, "\n"));
          buffer = "";
        }
        break;
      }
    }
  } catch (error) {
    try {
      await reader.cancel();
    } catch {
      // Preserve the original stream error.
    }
    if (controller.signal.aborted) {
      throw new UiApiError("서버 응답 시간이 초과되었습니다.", {
        code: "REQUEST_TIMEOUT",
        retryable: true,
        status: 0,
      });
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
    reader.releaseLock();
  }

  if (!completed) {
    throw new UiApiError("AI 응답 스트림이 완료되지 않았습니다.", {
      code: "INCOMPLETE_STREAM_RESPONSE",
      retryable: true,
      status: response.status,
    });
  }
  return completed;
}

function parseChatStreamFrame(frame: string): ChatStreamEvent | null {
  let eventName = "";
  const dataLines: string[] = [];
  frame.split("\n").forEach((line) => {
    if (!line || line.startsWith(":")) {
      return;
    }
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value =
      separator < 0
        ? ""
        : line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") {
      eventName = value;
    } else if (field === "data") {
      dataLines.push(value);
    }
  });
  if (!eventName || !dataLines.length) {
    return null;
  }

  let value: unknown;
  try {
    value = JSON.parse(dataLines.join("\n"));
  } catch {
    throw new UiApiError("AI 응답 스트림을 해석하지 못했습니다.", {
      code: "INVALID_STREAM_RESPONSE",
      retryable: false,
      status: 200,
    });
  }
  if (!isRecord(value)) {
    throw new UiApiError("AI 응답 스트림 형식이 올바르지 않습니다.", {
      code: "INVALID_STREAM_RESPONSE",
      retryable: false,
      status: 200,
    });
  }
  if (eventName === "start") {
    if (typeof value.request_id !== "string") {
      throw invalidChatStreamError(
        "AI 응답 스트림 시작 식별자가 없습니다.",
        200,
      );
    }
    return {
      type: "start",
      request_id: value.request_id,
      status: "processing",
    };
  }
  if (
    eventName === "text_delta" &&
    typeof value.request_id === "string" &&
    typeof value.sequence === "number" &&
    typeof value.text === "string"
  ) {
    return {
      type: "text_delta",
      request_id: value.request_id,
      sequence: value.sequence,
      text: value.text,
    };
  }
  if (eventName === "completed" && isChatSyncData(value)) {
    return { type: "completed", data: value };
  }
  if (
    eventName === "error" &&
    typeof value.request_id === "string" &&
    typeof value.code === "string" &&
    typeof value.message === "string" &&
    typeof value.retryable === "boolean"
  ) {
    return {
      type: "error",
      request_id: value.request_id,
      error: {
        code: value.code,
        message: value.message,
        retryable: value.retryable,
        details: isRecord(value.details) ? value.details : null,
      },
    };
  }
  throw new UiApiError("알 수 없는 AI 응답 스트림 이벤트입니다.", {
    code: "INVALID_STREAM_RESPONSE",
    retryable: false,
    status: 200,
  });
}

function invalidChatStreamError(
  message: string,
  status: number,
): UiApiError {
  return new UiApiError(message, {
    code: "INVALID_STREAM_RESPONSE",
    retryable: false,
    status,
  });
}

function isChatSyncData(value: unknown): value is ChatSyncData {
  if (!isRecord(value)) {
    return false;
  }
  if (
    typeof value.request_id === "string" &&
    typeof value.user_message_id === "string" &&
    typeof value.user_sort_sequence === "number" &&
    Number.isSafeInteger(value.user_sort_sequence) &&
    value.user_sort_sequence > 0 &&
    typeof value.assistant_message_id === "string" &&
    typeof value.assistant_sort_sequence === "number" &&
    Number.isSafeInteger(value.assistant_sort_sequence) &&
    value.assistant_sort_sequence > 0 &&
    (value.message_type === "text" ||
      value.message_type === "selection_box" ||
      value.message_type === "input_box") &&
    isChatMessageContent(value.message) &&
    typeof value.message_at === "string" &&
    typeof value.display_message_at === "string"
  ) {
    if (value.message_type === "text") {
      return typeof value.message.text === "string";
    }
    if (value.message_type === "selection_box") {
      return (
        Array.isArray(value.message.selections) &&
        value.message.selections.length > 0
      );
    }
    return (
      Array.isArray(value.message.inputs) &&
      value.message.inputs.length > 0
    );
  }
  return false;
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableNumber(value: unknown): value is number | null {
  return (
    value === null ||
    (typeof value === "number" && Number.isFinite(value))
  );
}

function isStringArrayOrNull(value: unknown): value is string[] | null {
  return (
    value === null ||
    (Array.isArray(value) &&
      value.every((item) => typeof item === "string"))
  );
}

function isChatMessageContent(
  value: unknown,
): value is ChatSyncData["message"] {
  if (
    !isRecord(value) ||
    !isNullableString(value.message_title) ||
    !isNullableString(value.text) ||
    !isStringArrayOrNull(value.selections)
  ) {
    return false;
  }
  if (
    value.tables !== null &&
    (!Array.isArray(value.tables) ||
      !value.tables.every(
        (table) =>
          isRecord(table) &&
          isNullableString(table.table_title) &&
          Array.isArray(table.rows) &&
          table.rows.every(
            (row) =>
              isRecord(row) &&
              typeof row.column === "string" &&
              typeof row.value === "string",
          ),
      ))
  ) {
    return false;
  }
  return (
    value.inputs === null ||
    (Array.isArray(value.inputs) &&
      value.inputs.every(
        (input) =>
          isRecord(input) &&
          (input.type === "number" || input.type === "dropdown") &&
          typeof input.label === "string" &&
          (input.value === null ||
            typeof input.value === "string" ||
            (typeof input.value === "number" &&
              Number.isFinite(input.value))) &&
          isRecord(input.options) &&
          isNullableString(input.options.unit) &&
          isNullableNumber(input.options.lower) &&
          isNullableNumber(input.options.upper) &&
          isStringArrayOrNull(input.options.selections),
      ))
  );
}

async function streamHttpError(response: Response): Promise<UiApiError> {
  let value: unknown = null;
  try {
    value = await response.json();
  } catch {
    // Use the status-derived error below.
  }
  if (isApiEnvelope<never>(value) && !value.success) {
    return new UiApiError(value.error.message, {
      code: value.error.code,
      details: value.error.details,
      retryable: value.error.retryable,
      status: response.status,
    });
  }
  return new UiApiError("Backend 서버가 스트림 요청을 거부했습니다.", {
    code: "HTTP_ERROR",
    retryable: isRetryableHttpStatus(response.status),
    status: response.status,
  });
}

export const uiApi = {
  dashboard(date?: string): Promise<DashboardData> {
    const query = date ? `?date=${encodeURIComponent(date)}` : "";
    return request<DashboardData>(`/dashboard${query}`, {
      timeoutMs: READINESS_REQUEST_TIMEOUT_MS,
    });
  },

  status(): Promise<SystemStatusData> {
    return request<SystemStatusData>("/status", {
      timeoutMs: READINESS_REQUEST_TIMEOUT_MS,
    });
  },

  resetTestbed(requestId: string): Promise<TestbedResetData> {
    return request<TestbedResetData>("/testbed/reset", {
      method: "POST",
      body: JSON.stringify({
        request_id: requestId,
        confirm: true,
      }),
    });
  },

  scenarios(): Promise<ScenarioListData> {
    return request<ScenarioListData>("/medication-scenarios");
  },

  applyScenario(
    scenarioId: string,
    scheduleDate: string,
    requestId: string = newRequestId(),
  ): Promise<ScenarioApplyData> {
    return request<ScenarioApplyData>("/medication-scenarios/apply", {
      method: "POST",
      body: JSON.stringify({
        request_id: requestId,
        scenario_id: scenarioId,
        schedule_date: scheduleDate,
      }),
    });
  },

  advanceClock(
    minutes: 30 | 180,
    requestId: string,
  ): Promise<{ clock: SimulationClock }> {
    return request<{ clock: SimulationClock }>("/clock/advance", {
      method: "POST",
      body: JSON.stringify({ request_id: requestId, minutes }),
    });
  },

  playClock(
    speedMultiplier: Exclude<SimulationSpeed, 0> = 60,
  ): Promise<{ clock: SimulationClock }> {
    return request<{ clock: SimulationClock }>("/clock/play", {
      method: "POST",
      body: JSON.stringify({ speed_multiplier: speedMultiplier }),
    });
  },

  pauseClock(): Promise<{ clock: SimulationClock }> {
    return request<{ clock: SimulationClock }>("/clock/pause", {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  takeDose(doseEventId: string): Promise<{ dose: MedicationDose }> {
    return request<{ dose: MedicationDose }>(
      `/doses/${encodeURIComponent(doseEventId)}/take`,
      {
        method: "POST",
        body: JSON.stringify({}),
      },
    );
  },

  updatePolicy(
    policyKey: PolicyKey,
    enabled: boolean,
  ): Promise<{ policies: UiPolicies }> {
    return request<{ policies: UiPolicies }>(
      `/policies/${encodeURIComponent(policyKey)}`,
      {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      },
    );
  },

  acknowledgeAllNotifications(): Promise<{ acknowledged_count: number }> {
    return request<{ acknowledged_count: number }>("/notifications/ack-all", {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  chatHistory(
    beforeDate?: string,
    limitDays = 1,
    limitTurns = 50,
  ): Promise<ChatHistoryData> {
    const query = new URLSearchParams({
      limit_days: String(limitDays),
      limit_turns: String(limitTurns),
    });
    if (beforeDate) {
      query.set("before_date", beforeDate);
    }
    return request<ChatHistoryData>(`/chat/history?${query.toString()}`);
  },

  chatStream(
    payload: ChatSyncRequest,
    onEvent: (event: ChatStreamEvent) => void,
  ): Promise<ChatSyncData> {
    return streamChat(payload, onEvent);
  },

  feedback(payload: FeedbackRequest): Promise<FeedbackData> {
    return request<FeedbackData>("/chat/feedback", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
};

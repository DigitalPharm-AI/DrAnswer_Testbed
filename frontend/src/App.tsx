import { useCallback, useEffect, useRef, useState } from "react";
import { uiApi, UiApiError } from "./api/client";
import type {
  AiServerStatus,
  BackendServerStatus,
  ChatSyncData,
  ClientChatHistoryDay,
  ClientChatHistoryMessage,
  DashboardData,
  FeedbackReaction,
  MedicationScenario,
  PolicyKey,
  RequestedReturnType,
  SystemStatusData,
} from "./api/contracts";
import ChatPage from "./components/ChatPage";
import HomePage from "./components/HomePage";
import { formatKoreaClock, koreaDateKey } from "./utils/koreaTime";
import { newRequestId } from "./utils/publicIds";

type ActiveTab = "home" | "chat";
type ToastKind = "success" | "neutral" | "notification" | "warning";
type RuntimeLoadState = "loading" | "ready" | "error";

interface RuntimeLoadFailure {
  area: string;
  message: string;
}

interface PendingChatRequest {
  clientMessageId: string;
  requestId: string;
  message: string;
  displayMessage: string;
  requestedReturnType: RequestedReturnType;
  sourceMessageId: string | null;
  createdAt: string;
}

const DASHBOARD_RECONCILE_INTERVAL_MS = 3_000;
const SERVICE_STATUS_INTERVAL_MS = 10_000;

type DisplayServiceStatus =
  | AiServerStatus
  | BackendServerStatus
  | "checking"
  | "unknown";

interface DisplaySystemStatus {
  backend_server: {
    status: DisplayServiceStatus;
    checked_at: string | null;
    evidence: string[];
  };
  ai_server: {
    status: DisplayServiceStatus;
    checked_at: string | null;
    evidence: string[];
  };
}

const INITIAL_SYSTEM_STATUS: DisplaySystemStatus = {
  backend_server: {
    status: "checking",
    checked_at: null,
    evidence: [],
  },
  ai_server: { status: "checking", checked_at: null, evidence: [] },
};

function RuntimeLoadPanel({
  state,
  failures,
  onRetry,
}: {
  state: Exclude<RuntimeLoadState, "ready">;
  failures: RuntimeLoadFailure[];
  onRetry: () => void;
}) {
  if (state === "loading") {
    return (
      <section
        className="panel runtime-state-panel is-loading"
        aria-live="polite"
        aria-busy="true"
      >
        <span className="runtime-loading-spinner" aria-hidden="true" />
        <div>
          <h2>Backend 데이터를 불러오는 중입니다.</h2>
          <p>복약 일정과 대화 기록을 확인하고 있습니다.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="panel runtime-state-panel is-error" role="alert">
      <div>
        <h2>Backend 연결을 확인해 주세요.</h2>
        <p>
          실제 데이터를 불러오지 못해 화면을 표시하지 않았습니다.
        </p>
        <ul>
          {failures.map((failure) => (
            <li key={failure.area}>
              <strong>{failure.area}</strong>: {failure.message}
            </li>
          ))}
        </ul>
      </div>
      <button className="compact-button" type="button" onClick={onRetry}>
        다시 연결
      </button>
    </section>
  );
}

function serviceStatusLabel(status: DisplayServiceStatus): string {
  switch (status) {
    case "ready":
      return "정상";
    case "not_ready":
      return "점검 필요";
    case "degraded":
      return "일부 장애";
    case "failed":
      return "오류";
    case "timeout":
      return "시간 초과";
    case "unreachable":
      return "연결 안 됨";
    case "incompatible":
      return "응답 오류";
    case "unknown":
      return "확인 불가";
    case "checking":
      return "확인 중";
  }
}

function ServiceStatusIndicator({
  label,
  status,
  checkedAt,
  evidence,
}: {
  label: string;
  status: DisplayServiceStatus;
  checkedAt: string | null;
  evidence: string[];
}) {
  const checkedLabel = checkedAt
    ? new Date(checkedAt).toLocaleString("ko-KR", {
        timeZone: "Asia/Seoul",
      })
    : "아직 확인하지 않음";
  const evidenceLabel = evidence.length
    ? `\n근거: ${evidence.join(" · ")}`
    : "";
  return (
    <span
      className={`connection-state service-state is-${status}`}
      data-status={status}
      title={`마지막 확인: ${checkedLabel}${evidenceLabel}`}
    >
      <span aria-hidden="true" />
      {label} {serviceStatusLabel(status)}
    </span>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof UiApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "요청을 처리하지 못했습니다.";
}

function userDisplayMessage(
  message: string,
  requestedReturnType: RequestedReturnType,
): string {
  if (requestedReturnType !== "input_box") {
    return message;
  }
  try {
    const values = JSON.parse(message) as Record<string, unknown>;
    return Object.entries(values)
      .map(([label, value]) => `${label}: ${String(value)}`)
      .join(" · ");
  } catch {
    return message;
  }
}

function appendMessages(
  days: ClientChatHistoryDay[],
  targetDate: string,
  messages: ClientChatHistoryMessage[],
): ClientChatHistoryDay[] {
  return mergeChatHistoryDays(days, [{ date: targetDate, messages }]);
}

function compareHistoryMessages(
  left: ClientChatHistoryMessage,
  right: ClientChatHistoryMessage,
): number {
  const leftSequence =
    typeof left.sort_sequence === "number" &&
    Number.isSafeInteger(left.sort_sequence)
      ? left.sort_sequence
      : null;
  const rightSequence =
    typeof right.sort_sequence === "number" &&
    Number.isSafeInteger(right.sort_sequence)
      ? right.sort_sequence
      : null;
  if (leftSequence !== null && rightSequence !== null) {
    const sequenceOrder = leftSequence - rightSequence;
    if (sequenceOrder) {
      return sequenceOrder;
    }
    return left.message_id.localeCompare(right.message_id);
  }
  if (leftSequence !== null) {
    return -1;
  }
  if (rightSequence !== null) {
    return 1;
  }

  // Optimistic rows have no server-assigned sort_sequence until persistence.
  const createdAtOrder = left.created_at.localeCompare(right.created_at);
  if (createdAtOrder) {
    return createdAtOrder;
  }
  if (
    left.role === "assistant" &&
    right.role === "user" &&
    right.source_message_id === left.message_id
  ) {
    return -1;
  }
  if (
    left.role === "user" &&
    right.role === "assistant" &&
    left.source_message_id === right.message_id
  ) {
    return 1;
  }
  const roleOrder =
    Number(left.role === "assistant") - Number(right.role === "assistant");
  return roleOrder || left.message_id.localeCompare(right.message_id);
}

/**
 * Backend history pages can overlap while new messages are being written.
 * Keep the already-rendered copy authoritative, but always rebuild a stable
 * date/message ordering and remove duplicate `(date, message_id)` entries.
 */
function mergeChatHistoryDays(
  current: ClientChatHistoryDay[],
  incoming: ClientChatHistoryDay[],
): ClientChatHistoryDay[] {
  const messagesByDate = new Map<
    string,
    Map<string, ClientChatHistoryMessage>
  >();

  const addDays = (days: ClientChatHistoryDay[]) => {
    days.forEach((day) => {
      const messages =
        messagesByDate.get(day.date) ??
        new Map<string, ClientChatHistoryMessage>();
      day.messages.forEach((message) => {
        messages.set(message.message_id, message);
      });
      messagesByDate.set(day.date, messages);
    });
  };

  // A newly fetched Backend row is canonical when pages overlap. Client-only
  // optimistic rows have distinct `client:` ids and therefore remain intact.
  addDays(current);
  addDays(incoming);

  return [...messagesByDate.entries()]
    .map(([date, messages]) => ({
      date,
      messages: [...messages.values()].sort(compareHistoryMessages),
    }))
    .filter((day) => day.messages.length > 0)
    .sort((left, right) => left.date.localeCompare(right.date));
}

function setOptimisticDelivery(
  days: ClientChatHistoryDay[],
  clientMessageId: string,
  deliveryStatus: "sending" | "failed",
  deliveryError: string | null,
  deliveryRetryable: boolean,
): ClientChatHistoryDay[] {
  return days.map((day) => ({
    ...day,
    messages: day.messages.map((message) =>
      message.message_id === clientMessageId
        ? {
            ...message,
            processing_status: deliveryStatus,
            delivery_status: deliveryStatus,
            delivery_error: deliveryError,
            delivery_retryable: deliveryRetryable,
          }
        : message,
    ),
  }));
}

function settleOptimisticChat(
  days: ClientChatHistoryDay[],
  request: PendingChatRequest,
  response: ChatSyncData,
): ClientChatHistoryDay[] {
  const displayMessageAt = response.display_message_at;
  const userMessage: ClientChatHistoryMessage = {
    message_id: response.user_message_id,
    sort_sequence: response.user_sort_sequence,
    role: "user",
    source_message_id: request.sourceMessageId,
    response_message_id: null,
    message_type: null,
    message: request.displayMessage,
    content: null,
    created_at: displayMessageAt,
    processing_status: "completed",
    reaction: null,
    opinion_submitted: false,
    opinion_submitted_at: null,
  };
  const assistantMessage: ClientChatHistoryMessage = {
    message_id: response.assistant_message_id,
    sort_sequence: response.assistant_sort_sequence,
    role: "assistant",
    source_message_id: null,
    response_message_id: null,
    message_type: response.message_type,
    message: null,
    content: response.message,
    created_at: displayMessageAt,
    processing_status:
      response.message_type === "text" ? "completed" : "pending",
    reaction: null,
    opinion_submitted: false,
    opinion_submitted_at: null,
  };

  const withoutOptimistic = days
    .map((day) => ({
      ...day,
      messages: day.messages
        .filter(
          (message) =>
            message.message_id !== request.clientMessageId &&
            message.message_id !== streamingAssistantId(request.requestId),
        )
        .map((message) =>
          request.sourceMessageId &&
          message.message_id === request.sourceMessageId
            ? {
                ...message,
                processing_status: "answered",
                response_message_id: response.user_message_id,
              }
            : message,
        ),
    }))
    .filter((day) => day.messages.length > 0);
  const existingIds = new Set(
    withoutOptimistic.flatMap((day) =>
      day.messages.map((message) => message.message_id),
    ),
  );
  const confirmedMessages = [
    ...(existingIds.has(userMessage.message_id) ? [] : [userMessage]),
    ...(existingIds.has(assistantMessage.message_id)
      ? []
      : [assistantMessage]),
  ];

  if (!confirmedMessages.length) {
    return withoutOptimistic;
  }
  return appendMessages(
    withoutOptimistic,
    koreaDateKey(displayMessageAt),
    confirmedMessages,
  );
}

function streamingAssistantId(requestId: string): string {
  return `stream:${requestId}`;
}

function appendStreamingAssistantText(
  days: ClientChatHistoryDay[],
  request: PendingChatRequest,
  text: string,
): ClientChatHistoryDay[] {
  if (!text) {
    return days;
  }
  const messageId = streamingAssistantId(request.requestId);
  let found = false;
  const updated = days.map((day) => ({
    ...day,
    messages: day.messages.map((message) => {
      if (message.message_id !== messageId) {
        return message;
      }
      found = true;
      return {
        ...message,
        content: {
          message_title: null,
          text: `${message.content?.text ?? ""}${text}`,
          tables: null,
          selections: null,
          inputs: null,
        },
      };
    }),
  }));
  if (found) {
    return updated;
  }
  return appendMessages(updated, koreaDateKey(request.createdAt), [
    {
      message_id: messageId,
      sort_sequence: null,
      role: "assistant",
      source_message_id: null,
      response_message_id: null,
      message_type: "text",
      message: null,
      content: {
        message_title: null,
        text,
        tables: null,
        selections: null,
        inputs: null,
      },
      created_at: request.createdAt,
      processing_status: "streaming",
      reaction: null,
      opinion_submitted: false,
      opinion_submitted_at: null,
    },
  ]);
}

function removeStreamingAssistant(
  days: ClientChatHistoryDay[],
  requestId: string,
): ClientChatHistoryDay[] {
  const messageId = streamingAssistantId(requestId);
  return days
    .map((day) => ({
      ...day,
      messages: day.messages.filter(
        (message) => message.message_id !== messageId,
      ),
    }))
    .filter((day) => day.messages.length > 0);
}

export default function App() {
  const [activeTab, setActiveTab] = useState<ActiveTab>("home");
  const [runtimeLoadState, setRuntimeLoadState] =
    useState<RuntimeLoadState>("loading");
  const [runtimeLoadFailures, setRuntimeLoadFailures] = useState<
    RuntimeLoadFailure[]
  >([]);
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [scenarios, setScenarios] = useState<MedicationScenario[]>([]);
  const [chatDays, setChatDays] = useState<ClientChatHistoryDay[]>([]);
  const [nextBeforeDate, setNextBeforeDate] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [chatSending, setChatSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [chatErrorRetryable, setChatErrorRetryable] = useState(false);
  const [dashboardSyncError, setDashboardSyncError] = useState<string | null>(
    null,
  );
  const [statusSyncError, setStatusSyncError] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [chatDraft, setChatDraft] = useState("");
  const [missedDoseSourceMessageId, setMissedDoseSourceMessageId] = useState<
    string | null
  >(null);
  const [systemStatus, setSystemStatus] = useState<DisplaySystemStatus>(
    INITIAL_SYSTEM_STATUS,
  );
  const [toast, setToast] = useState<{
    message: string;
    kind: ToastKind;
  } | null>(null);
  const toastTimerRef = useRef<number | null>(null);
  const scenarioRequestRef = useRef<{ key: string; requestId: string } | null>(
    null,
  );
  const clockAdvanceRequestRef = useRef<{
    key: string;
    requestId: string;
  } | null>(null);
  const chatRequestsRef = useRef<Map<string, PendingChatRequest>>(new Map());
  const chatSendingRef = useRef(false);
  const activeChatRequestIdRef = useRef<string | null>(null);
  const feedbackRequestsRef = useRef<
    Map<string, { requestId: string; feedbackAt: string }>
  >(new Map());
  const reactionRequestsRef = useRef<
    Map<string, { requestId: string; feedbackAt: string }>
  >(new Map());
  const mountedRef = useRef(true);
  const dashboardRequestRef = useRef<Promise<DashboardData> | null>(null);
  const statusRequestRef = useRef<Promise<SystemStatusData> | null>(null);
  const resetRequestRef = useRef<string | null>(null);
  const resettingRef = useRef(false);

  const fetchDashboardOnce = useCallback((): Promise<DashboardData> => {
    if (dashboardRequestRef.current) {
      return dashboardRequestRef.current;
    }

    let request: Promise<DashboardData>;
    request = uiApi.dashboard().finally(() => {
      if (dashboardRequestRef.current === request) {
        dashboardRequestRef.current = null;
      }
    });
    dashboardRequestRef.current = request;
    return request;
  }, []);

  const markDashboardSyncFailure = useCallback((error: unknown) => {
    if (!mountedRef.current) {
      return;
    }
    const checkedAt = new Date().toISOString();
    setDashboardSyncError(errorMessage(error));
    setSystemStatus((current) => ({
      ...current,
      backend_server: {
        status:
          current.backend_server.status === "unreachable"
            ? "unreachable"
            : "failed",
        checked_at: checkedAt,
        evidence: [errorMessage(error)],
      },
    }));
  }, []);

  const reconcileDashboard = useCallback(
    async (requireFreshAfterActiveRequest = false) => {
      if (resettingRef.current) {
        return;
      }
      const activeRequest = dashboardRequestRef.current;
      if (activeRequest) {
        try {
          await activeRequest;
        } catch (error) {
          markDashboardSyncFailure(error);
        }
        if (!requireFreshAfterActiveRequest || !mountedRef.current) {
          return;
        }
      }

      try {
        const nextDashboard = await fetchDashboardOnce();
        if (mountedRef.current) {
          setDashboard(nextDashboard);
          setDashboardSyncError(null);
        }
      } catch (error) {
        markDashboardSyncFailure(error);
      }
    },
    [fetchDashboardOnce, markDashboardSyncFailure],
  );

  const refreshSystemStatus = useCallback(async () => {
    if (statusRequestRef.current) {
      return statusRequestRef.current;
    }

    let request: Promise<SystemStatusData>;
    request = uiApi.status().finally(() => {
      if (statusRequestRef.current === request) {
        statusRequestRef.current = null;
      }
    });
    statusRequestRef.current = request;
    try {
      const nextStatus = await request;
      if (mountedRef.current) {
        setSystemStatus(nextStatus);
        setStatusSyncError(null);
      }
      return nextStatus;
    } catch (error) {
      if (mountedRef.current) {
        const checkedAt = new Date().toISOString();
        setStatusSyncError(errorMessage(error));
        setSystemStatus({
          backend_server: {
            status: "unreachable",
            checked_at: checkedAt,
            evidence: [errorMessage(error)],
          },
          ai_server: {
            status: "unknown",
            checked_at: checkedAt,
            evidence: [
              "Backend 상태 API에 연결할 수 없어 AI 상태를 확인하지 못했습니다.",
            ],
          },
        });
      }
      throw error;
    }
  }, []);

  function showToast(message: string, kind: ToastKind = "success") {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
    }
    setToast({ message, kind });
    toastTimerRef.current = window.setTimeout(() => {
      setToast(null);
      toastTimerRef.current = null;
    }, 2800);
  }

  const loadApplicationData = useCallback(async (): Promise<boolean> => {
    chatRequestsRef.current.clear();
    chatSendingRef.current = false;
    activeChatRequestIdRef.current = null;
    feedbackRequestsRef.current.clear();
    reactionRequestsRef.current.clear();
    setRuntimeLoadState("loading");
    setRuntimeLoadFailures([]);
    setDashboard(null);
    setScenarios([]);
    setChatDays([]);
    setChatSending(false);
    setNextBeforeDate(null);
    setDashboardSyncError(null);
    setChatError(null);
    setChatErrorRetryable(false);
    setMissedDoseSourceMessageId(null);

    const [
      dashboardResult,
      scenarioResult,
      chatResult,
      _statusResult,
    ] =
      await Promise.allSettled([
        fetchDashboardOnce(),
        uiApi.scenarios(),
        uiApi.chatHistory(undefined, 1),
        refreshSystemStatus(),
      ]);
    if (!mountedRef.current) {
      return false;
    }

    const failures: RuntimeLoadFailure[] = [];
    if (dashboardResult.status === "rejected") {
      failures.push({
        area: "홈",
        message: errorMessage(dashboardResult.reason),
      });
    }
    if (scenarioResult.status === "rejected") {
      failures.push({
        area: "테스트 복약 일정",
        message: errorMessage(scenarioResult.reason),
      });
    }
    if (chatResult.status === "rejected") {
      failures.push({
        area: "대화",
        message: errorMessage(chatResult.reason),
      });
    }

    if (failures.length) {
      setRuntimeLoadFailures(failures);
      setRuntimeLoadState("error");
      return false;
    }
    if (
      dashboardResult.status !== "fulfilled" ||
      scenarioResult.status !== "fulfilled" ||
      chatResult.status !== "fulfilled"
    ) {
      setRuntimeLoadFailures([
        {
          area: "화면",
          message: "필수 Backend 응답을 확인할 수 없습니다.",
        },
      ]);
      setRuntimeLoadState("error");
      return false;
    }

    setDashboard(dashboardResult.value);
    setScenarios(scenarioResult.value.scenarios);
    setChatDays(mergeChatHistoryDays([], chatResult.value.days));
    setNextBeforeDate(chatResult.value.next_before_date);
    setRuntimeLoadState("ready");

    // refreshSystemStatus already updates the badges and the persistent
    // connection warning. It intentionally does not block usable Backend data.
    void _statusResult;
    return true;
  }, [fetchDashboardOnce, refreshSystemStatus]);

  useEffect(() => {
    mountedRef.current = true;
    void loadApplicationData();
    return () => {
      mountedRef.current = false;
    };
  }, [loadApplicationData]);

  useEffect(() => {
    if (runtimeLoadState !== "ready") {
      return;
    }
    const timer = window.setInterval(() => {
      void reconcileDashboard();
    }, DASHBOARD_RECONCILE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [reconcileDashboard, runtimeLoadState]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refreshSystemStatus().catch(() => undefined);
    }, SERVICE_STATUS_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refreshSystemStatus]);

  useEffect(() => {
    if (!dashboard?.clock.is_running || dashboardSyncError) {
      return;
    }
    const timer = window.setInterval(() => {
      setDashboard((current) =>
        current
          ? {
              ...current,
              clock: {
                ...current.clock,
                current_time: new Date(
                  new Date(current.clock.current_time).getTime() +
                    current.clock.speed_multiplier * 60_000,
                ).toISOString(),
              },
            }
          : current,
      );
    }, 1000);
    return () => window.clearInterval(timer);
  }, [
    dashboard?.clock.is_running,
    dashboard?.clock.speed_multiplier,
    dashboardSyncError,
  ]);

  useEffect(
    () => () => {
      if (toastTimerRef.current !== null) {
        window.clearTimeout(toastTimerRef.current);
      }
    },
    [],
  );

  async function advanceClock(minutes: 30 | 180) {
    const requestKey = String(minutes);
    if (clockAdvanceRequestRef.current?.key !== requestKey) {
      clockAdvanceRequestRef.current = {
        key: requestKey,
        requestId: newRequestId(),
      };
    }
    try {
      const result = await uiApi.advanceClock(
        minutes,
        clockAdvanceRequestRef.current.requestId,
      );
      setDashboard((current) =>
        current ? { ...current, clock: result.clock } : current,
      );
      clockAdvanceRequestRef.current = null;
      await reconcileDashboard(true);
      showToast(`시뮬레이션 시간을 ${minutes}분 진행했습니다.`);
    } catch (error) {
      if (
        error instanceof UiApiError &&
        error.code === "IDEMPOTENCY_CONFLICT"
      ) {
        clockAdvanceRequestRef.current = null;
      }
      showToast(errorMessage(error), "warning");
      throw error;
    }
  }

  async function setClockRunning(running: boolean) {
    try {
      const result = running
        ? await uiApi.playClock(60)
        : await uiApi.pauseClock();
      setDashboard((current) =>
        current ? { ...current, clock: result.clock } : current,
      );
      await reconcileDashboard(true);
      showToast(
        running
          ? "시뮬레이션을 시작했습니다."
          : "시뮬레이션을 일시 정지했습니다.",
      );
    } catch (error) {
      showToast(errorMessage(error), "warning");
      throw error;
    }
  }

  async function applyScenario(scenarioId: string) {
    if (!dashboard) {
      showToast("홈 데이터를 먼저 불러와 주세요.", "warning");
      return;
    }
    const scheduleDate = koreaDateKey(dashboard.clock.current_time);
    const requestKey = `${scenarioId}\u0000${scheduleDate}`;
    if (scenarioRequestRef.current?.key !== requestKey) {
      scenarioRequestRef.current = {
        key: requestKey,
        requestId: newRequestId(),
      };
    }
    try {
      const result = await uiApi.applyScenario(
        scenarioId,
        scheduleDate,
        scenarioRequestRef.current.requestId,
      );
      setDashboard((current) =>
        current
          ? {
              ...current,
              active_scenario: result.scenario,
              medications: result.medications,
            }
          : current,
      );
      scenarioRequestRef.current = null;
      await reconcileDashboard(true);
      showToast(`${result.scenario.name} 테스트 일정을 적용했습니다.`);
    } catch (error) {
      if (
        error instanceof UiApiError &&
        error.code === "IDEMPOTENCY_CONFLICT"
      ) {
        scenarioRequestRef.current = null;
      }
      showToast(errorMessage(error), "warning");
      throw error;
    }
  }

  async function takeDose(doseEventId: string) {
    try {
      const result = await uiApi.takeDose(doseEventId);
      setDashboard((current) =>
        current
          ? {
              ...current,
              medications: current.medications.map((dose) =>
                dose.dose_event_id === doseEventId ? result.dose : dose,
              ),
            }
          : current,
      );
      await reconcileDashboard(true);
      showToast(`${result.dose.medication_name} 복용을 완료로 기록했습니다.`);
    } catch (error) {
      showToast(errorMessage(error), "warning");
      throw error;
    }
  }

  async function updatePolicy(policy: PolicyKey, enabled: boolean) {
    try {
      const result = await uiApi.updatePolicy(policy, enabled);
      setDashboard((current) =>
        current ? { ...current, policies: result.policies } : current,
      );
      await reconcileDashboard(true);
      showToast(enabled ? "알림 정책을 켰습니다." : "알림 정책을 껐습니다.");
    } catch (error) {
      showToast(errorMessage(error), "warning");
      throw error;
    }
  }

  async function acknowledgeNotifications() {
    try {
      await uiApi.acknowledgeAllNotifications();
      setDashboard((current) =>
        current ? { ...current, notifications: [] } : current,
      );
      await reconcileDashboard(true);
      showToast("모든 알림을 확인 처리했습니다.", "notification");
    } catch (error) {
      showToast(errorMessage(error), "warning");
      throw error;
    }
  }

  async function resetTestbed() {
    if (resettingRef.current || chatSendingRef.current) {
      if (chatSendingRef.current) {
        showToast(
          "AI 응답이 완료된 뒤 테스트 환경을 초기화해 주세요.",
          "warning",
        );
      }
      return;
    }
    resettingRef.current = true;
    setResetting(true);
    if (!resetRequestRef.current) {
      resetRequestRef.current = newRequestId();
    }

    try {
      const activeDashboardRequest = dashboardRequestRef.current;
      if (activeDashboardRequest) {
        await activeDashboardRequest.catch(() => undefined);
      }

      await uiApi.resetTestbed(resetRequestRef.current);
      resetRequestRef.current = null;
      scenarioRequestRef.current = null;
      clockAdvanceRequestRef.current = null;
      chatRequestsRef.current.clear();
      chatSendingRef.current = false;
      activeChatRequestIdRef.current = null;
      feedbackRequestsRef.current.clear();
      reactionRequestsRef.current.clear();
      setChatDraft("");
      setChatError(null);
      setChatErrorRetryable(false);
      setMissedDoseSourceMessageId(null);
      setActiveTab("home");

      const refreshSucceeded = await loadApplicationData();
      showToast(
        refreshSucceeded
          ? "테스트 환경을 초기화했습니다."
          : "초기화했지만 Backend 데이터를 다시 불러오지 못했습니다.",
        refreshSucceeded ? "success" : "warning",
      );
    } catch (error) {
      if (
        error instanceof UiApiError &&
        error.code === "IDEMPOTENCY_CONFLICT"
      ) {
        resetRequestRef.current = null;
      }
      showToast(errorMessage(error), "warning");
      throw error;
    } finally {
      resettingRef.current = false;
      setResetting(false);
    }
  }

  async function loadPreviousHistory() {
    if (historyLoading || !nextBeforeDate) {
      return;
    }
    setHistoryLoading(true);
    try {
      const result = await uiApi.chatHistory(nextBeforeDate, 1);
      if (result.days.length) {
        setChatDays((current) =>
          mergeChatHistoryDays(current, result.days),
        );
      }
      setNextBeforeDate(result.next_before_date);
    } catch (error) {
      showToast(errorMessage(error), "warning");
      throw error;
    } finally {
      setHistoryLoading(false);
    }
  }

  async function openNotificationChat(messageId: string) {
    setHistoryLoading(true);
    try {
      const result = await uiApi.chatHistory(undefined, 7);
      setChatDays(mergeChatHistoryDays([], result.days));
      setNextBeforeDate(result.next_before_date);
      setMissedDoseSourceMessageId(messageId);
      setActiveTab("chat");
    } catch (error) {
      showToast(errorMessage(error), "warning");
      throw error;
    } finally {
      setHistoryLoading(false);
    }
  }

  async function performChatRequest(request: PendingChatRequest) {
    if (chatSendingRef.current) {
      return;
    }
    chatSendingRef.current = true;
    activeChatRequestIdRef.current = request.requestId;
    setChatSending(true);
    setChatError(null);
    setChatErrorRetryable(false);
    setChatDays((current) =>
      setOptimisticDelivery(
        current,
        request.clientMessageId,
        "sending",
        null,
        false,
      ),
    );

    let pendingText = "";
    let animationFrameId: number | null = null;
    const flushPendingText = () => {
      animationFrameId = null;
      if (
        !pendingText ||
        activeChatRequestIdRef.current !== request.requestId
      ) {
        pendingText = "";
        return;
      }
      const text = pendingText;
      pendingText = "";
      setChatDays((current) =>
        appendStreamingAssistantText(current, request, text),
      );
    };
    const cancelPendingText = () => {
      if (animationFrameId !== null) {
        window.cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
      }
      pendingText = "";
    };

    try {
      const response = await uiApi.chatStream(
        {
          message: request.message,
          requested_return_type: request.requestedReturnType,
          request_id: request.requestId,
          source_message_id: request.sourceMessageId,
        },
        (event) => {
          if (activeChatRequestIdRef.current !== request.requestId) {
            return;
          }
          if (event.type === "text_delta") {
            pendingText += event.text;
            if (animationFrameId === null) {
              animationFrameId =
                window.requestAnimationFrame(flushPendingText);
            }
          } else if (event.type === "completed") {
            if (animationFrameId !== null) {
              window.cancelAnimationFrame(animationFrameId);
            }
            flushPendingText();
          }
        },
      );
      if (activeChatRequestIdRef.current !== request.requestId) {
        return;
      }
      setChatDays((current) =>
        settleOptimisticChat(current, request, response),
      );
      chatRequestsRef.current.delete(request.clientMessageId);
      void reconcileDashboard(true);
      if (request.requestedReturnType === "selection_box") {
        showToast("선택 응답이 반영되었습니다.");
      } else if (request.requestedReturnType === "input_box") {
        showToast("입력 응답이 반영되었습니다.");
      }
      if (
        missedDoseSourceMessageId &&
        request.sourceMessageId === missedDoseSourceMessageId
      ) {
        setMissedDoseSourceMessageId(null);
      }
    } catch (error) {
      cancelPendingText();
      if (activeChatRequestIdRef.current !== request.requestId) {
        return;
      }
      const failureMessage = errorMessage(error);
      const retryable =
        error instanceof UiApiError && error.retryable;
      setChatDays((current) =>
        setOptimisticDelivery(
          removeStreamingAssistant(current, request.requestId),
          request.clientMessageId,
          "failed",
          failureMessage,
          retryable,
        ),
      );
      if (!retryable) {
        chatRequestsRef.current.delete(request.clientMessageId);
      }
      setChatError(failureMessage);
      setChatErrorRetryable(retryable);
      showToast("AI 응답 요청에 실패했습니다.", "warning");
      throw error;
    } finally {
      cancelPendingText();
      if (activeChatRequestIdRef.current === request.requestId) {
        activeChatRequestIdRef.current = null;
        chatSendingRef.current = false;
        setChatSending(false);
      }
    }
  }

  async function sendChat(
    message: string,
    requestedReturnType: RequestedReturnType,
    sourceMessageId?: string,
  ) {
    if (chatSendingRef.current) {
      return;
    }
    const requestId = newRequestId();
    const createdAt =
      dashboard?.clock.current_time ?? new Date().toISOString();
    const clientMessageId = `client:${requestId}`;
    const request: PendingChatRequest = {
      clientMessageId,
      requestId,
      message,
      displayMessage: userDisplayMessage(message, requestedReturnType),
      requestedReturnType,
      sourceMessageId:
        sourceMessageId ?? missedDoseSourceMessageId ?? null,
      createdAt,
    };
    chatRequestsRef.current.set(clientMessageId, request);
    setChatDays((current) =>
      appendMessages(current, koreaDateKey(createdAt), [
        {
          message_id: clientMessageId,
          sort_sequence: null,
          role: "user",
          source_message_id: request.sourceMessageId,
          response_message_id: null,
          message_type: null,
          message: request.displayMessage,
          content: null,
          created_at: createdAt,
          processing_status: "sending",
          reaction: null,
          opinion_submitted: false,
          opinion_submitted_at: null,
          client_request_id: requestId,
          delivery_status: "sending",
          delivery_error: null,
          delivery_retryable: false,
        },
      ]),
    );
    setChatDraft("");
    await performChatRequest(request);
  }

  async function retryChat(clientMessageId: string) {
    const request = chatRequestsRef.current.get(clientMessageId);
    if (!request || chatSendingRef.current) {
      return;
    }
    await performChatRequest(request);
  }

  async function sendFeedback(
    assistantMessageId: string,
    opinionText: string,
  ) {
    const requestKey = `${assistantMessageId}\u0000${opinionText}`;
    let request = feedbackRequestsRef.current.get(requestKey);
    if (!request) {
      request = {
        requestId: newRequestId(),
        feedbackAt: new Date().toISOString(),
      };
      feedbackRequestsRef.current.set(requestKey, request);
    }
    try {
      const result = await uiApi.feedback({
        request_id: request.requestId,
        assistant_message_id: assistantMessageId,
        opinion_text: opinionText,
        feedback_at: request.feedbackAt,
      });
      feedbackRequestsRef.current.delete(requestKey);
      setChatDays((current) =>
        current.map((day) => ({
          ...day,
          messages: day.messages.map((message) =>
            message.message_id === assistantMessageId
              ? {
                  ...message,
                  reaction: result.reaction,
                  opinion_submitted: result.opinion_submitted,
                  opinion_submitted_at: result.opinion_submitted_at,
                }
              : message,
          ),
        })),
      );
      showToast("소중한 의견을 전송했습니다.", "neutral");
    } catch (error) {
      if (
        error instanceof UiApiError &&
        error.code === "IDEMPOTENCY_CONFLICT"
      ) {
        feedbackRequestsRef.current.delete(requestKey);
      }
      showToast(errorMessage(error), "warning");
      throw error;
    }
  }

  async function sendReaction(
    assistantMessageId: string,
    reaction: FeedbackReaction,
  ): Promise<FeedbackReaction> {
    const requestKey = `${assistantMessageId}\u0000${reaction}`;
    let request = reactionRequestsRef.current.get(requestKey);
    if (!request) {
      request = {
        requestId: newRequestId(),
        feedbackAt: new Date().toISOString(),
      };
      reactionRequestsRef.current.set(requestKey, request);
    }
    try {
      const result = await uiApi.feedback({
        request_id: request.requestId,
        assistant_message_id: assistantMessageId,
        reaction,
        feedback_at: request.feedbackAt,
      });
      if (
        result.reaction !== "like" &&
        result.reaction !== "dislike"
      ) {
        throw new UiApiError("서버의 반응 응답을 확인할 수 없습니다.", {
          code: "INVALID_RESPONSE",
          retryable: false,
          status: 200,
        });
      }
      const nextReaction = result.reaction;
      reactionRequestsRef.current.delete(requestKey);
      setChatDays((current) =>
        current.map((day) => ({
          ...day,
          messages: day.messages.map((message) =>
            message.message_id === assistantMessageId
              ? { ...message, reaction: nextReaction }
              : message,
          ),
        })),
      );
      showToast("답변 반응을 저장했습니다.", "neutral");
      return nextReaction;
    } catch (error) {
      showToast(errorMessage(error), "warning");
      throw error;
    }
  }

  function retrySystemStatus() {
    setSystemStatus({
      backend_server: {
        status: "checking",
        checked_at: null,
        evidence: [],
      },
      ai_server: {
        status: "checking",
        checked_at: null,
        evidence: [],
      },
    });
    void refreshSystemStatus().catch(() => undefined);
  }

  const formattedClock = dashboard
    ? formatKoreaClock(dashboard.clock.current_time)
    : "";

  return (
    <>
      <div
        id="app-toast"
        className="app-toast"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-kind={toast?.kind}
        hidden={!toast}
      >
        {toast?.message}
      </div>

      <header className="top-time-bar">
        <div className="top-time-inner">
          <div className="top-time-copy">
            {dashboard ? (
              <>
                <div
                  className="top-time-controls"
                  aria-label="시뮬레이션 제어"
                >
                  <button
                    className={`control-button${
                      dashboard.clock.is_running ? " is-active" : " ghost"
                    }`}
                    type="button"
                    disabled={resetting || Boolean(dashboardSyncError)}
                    onClick={() =>
                      void setClockRunning(true).catch(() => undefined)
                    }
                  >
                    시작
                  </button>
                  <button
                    className={`control-button${
                      dashboard.clock.is_running ? " ghost" : " is-active"
                    }`}
                    type="button"
                    disabled={resetting || Boolean(dashboardSyncError)}
                    onClick={() =>
                      void setClockRunning(false).catch(() => undefined)
                    }
                  >
                    정지
                  </button>
                  <span className="status-pill">
                    {dashboard.clock.is_running ? "실행 중" : "일시 정지"}
                  </span>
                </div>
                <time
                  className="top-time-value"
                  dateTime={dashboard.clock.current_time}
                >
                  {formattedClock}
                </time>
                <span className="speed-label">배속 60분/초</span>
              </>
            ) : (
              <span className="top-time-loading" role="status">
                시뮬레이션 시간 확인 중
              </span>
            )}
            <div
              className="top-time-meta"
              aria-label="서버 상태"
              aria-live="polite"
            >
              <ServiceStatusIndicator
                label="Backend"
                status={systemStatus.backend_server.status}
                checkedAt={systemStatus.backend_server.checked_at}
                evidence={systemStatus.backend_server.evidence}
              />
              <ServiceStatusIndicator
                label="AI Server"
                status={systemStatus.ai_server.status}
                checkedAt={systemStatus.ai_server.checked_at}
                evidence={systemStatus.ai_server.evidence}
              />
            </div>
          </div>
        </div>
      </header>

      <div className="page-shell">
        <header className="hero">
          <div className="hero-main">
            <p className="eyebrow">DRANSWER MEDICATION CARE</p>
            <h1>닥터앤서 AI 복약 케어 테스트</h1>
          </div>
        </header>

        <div className="app-navigation">
          <nav className="app-tabs" aria-label="주요 화면">
            <button
              className={`app-tab${activeTab === "home" ? " is-active" : ""}`}
              type="button"
              role="tab"
              aria-selected={activeTab === "home"}
              aria-controls="home-page"
              disabled={runtimeLoadState !== "ready"}
              onClick={() => setActiveTab("home")}
            >
              HOME
            </button>
            <button
              className={`app-tab${activeTab === "chat" ? " is-active" : ""}`}
              type="button"
              role="tab"
              aria-selected={activeTab === "chat"}
              aria-controls="chat-page"
              disabled={runtimeLoadState !== "ready"}
              onClick={() => setActiveTab("chat")}
            >
              Chat
            </button>
          </nav>
          {activeTab === "home" ? (
            <section
              className="home-start-guide"
              aria-label="테스트 시작 방법"
            >
              <span className="home-start-guide-label">시작 방법</span>
              <div className="home-start-guide-step">
                <span className="home-start-guide-number">1.</span>
                <span>
                  <strong>테스트 복약 일정 설정</strong>
                  <small>
                    왼쪽 아래의 ‘테스트 복약 일정 설정’ 버튼에서 사용할
                    일정을 선택하세요.
                  </small>
                </span>
              </div>
              <span
                className="home-start-guide-arrow"
                aria-hidden="true"
              >
                →
              </span>
              <div className="home-start-guide-step">
                <span className="home-start-guide-number">2.</span>
                <span>
                  <strong>왼쪽 위 시작 버튼 선택</strong>
                  <small>
                    왼쪽 위의 ‘시작’ 버튼을 누르면 1초마다 60분씩
                    진행됩니다.
                  </small>
                </span>
              </div>
            </section>
          ) : null}
        </div>

        <main>
          {runtimeLoadState !== "ready" || !dashboard ? (
            <RuntimeLoadPanel
              state={
                runtimeLoadState === "ready"
                  ? "error"
                  : runtimeLoadState
              }
              failures={
                runtimeLoadFailures.length
                  ? runtimeLoadFailures
                  : [
                      {
                        area: "화면",
                        message: "필수 Backend 데이터가 없습니다.",
                      },
                    ]
              }
              onRetry={() => void loadApplicationData()}
            />
          ) : (
            <>
              {dashboardSyncError || statusSyncError ? (
                <div
                  className="runtime-warning-stack"
                  aria-live="polite"
                >
                  {dashboardSyncError ? (
                    <section className="runtime-warning">
                      <div>
                        <strong>홈 데이터 갱신 실패</strong>
                        <p>
                          표시 중인 정보가 최신이 아닐 수 있습니다.{" "}
                          {dashboardSyncError}
                        </p>
                      </div>
                      <button
                        className="compact-button ghost"
                        type="button"
                        onClick={() => void reconcileDashboard(true)}
                      >
                        다시 불러오기
                      </button>
                    </section>
                  ) : null}
                  {statusSyncError ? (
                    <section className="runtime-warning">
                      <div>
                        <strong>서버 상태 확인 실패</strong>
                        <p>{statusSyncError}</p>
                      </div>
                      <button
                        className="compact-button ghost"
                        type="button"
                        onClick={retrySystemStatus}
                      >
                        상태 다시 확인
                      </button>
                    </section>
                  ) : null}
                </div>
              ) : null}

              <section
                id="home-page"
                className={`page-tab-panel${
                  activeTab === "home" ? " is-active" : ""
                }`}
                role="tabpanel"
                aria-label="홈 화면"
                hidden={activeTab !== "home"}
              >
                <HomePage
                  dashboard={dashboard}
                  scenarios={scenarios}
                  onAdvanceClock={advanceClock}
                  onApplyScenario={applyScenario}
                  onTakeDose={takeDose}
                  onUpdatePolicy={updatePolicy}
                  onAcknowledgeNotifications={acknowledgeNotifications}
                  onOpenNotificationChat={openNotificationChat}
                  onResetTestbed={resetTestbed}
                  disabled={
                    resetting ||
                    chatSending ||
                    Boolean(dashboardSyncError)
                  }
                />
              </section>

              <section
                id="chat-page"
                className={`page-tab-panel${
                  activeTab === "chat" ? " is-active" : ""
                }`}
                role="tabpanel"
                aria-label="AI 채팅 화면"
                hidden={activeTab !== "chat"}
              >
                <ChatPage
                  active={activeTab === "chat"}
                  days={chatDays}
                  historyLoading={historyLoading}
                  chatSending={chatSending}
                  chatError={chatError}
                  chatErrorRetryable={chatErrorRetryable}
                  draft={chatDraft}
                  onDraftChange={setChatDraft}
                  onLoadPrevious={loadPreviousHistory}
                  onSend={sendChat}
                  onRetry={retryChat}
                  onFeedback={sendFeedback}
                  onReaction={sendReaction}
                  showToast={showToast}
                />
              </section>
            </>
          )}
        </main>
      </div>
    </>
  );
}

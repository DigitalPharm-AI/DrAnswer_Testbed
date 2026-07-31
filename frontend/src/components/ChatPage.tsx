import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import type {
  ClientChatHistoryDay,
  ClientChatHistoryMessage,
  ChatInput,
  FeedbackReaction,
  RequestedReturnType,
} from "../api/contracts";
import {
  formatKoreanCalendarDate,
  formatKoreanMessageTime,
} from "../utils/koreaTime";
import AssistantMarkdown from "./AssistantMarkdown";

interface ChatPageProps {
  active: boolean;
  days: ClientChatHistoryDay[];
  historyLoading: boolean;
  chatSending: boolean;
  chatError: string | null;
  chatErrorRetryable: boolean;
  draft: string;
  onDraftChange: (value: string) => void;
  onLoadPrevious: () => Promise<void>;
  onSend: (
    message: string,
    requestedReturnType: RequestedReturnType,
    sourceMessageId?: string,
  ) => Promise<void>;
  onRetry: (clientMessageId: string) => Promise<void>;
  onFeedback: (assistantMessageId: string, opinion: string) => Promise<void>;
  onReaction: (
    assistantMessageId: string,
    reaction: FeedbackReaction,
  ) => Promise<FeedbackReaction>;
  showToast: (
    message: string,
    kind?: "success" | "neutral" | "notification" | "warning",
  ) => void;
}

interface TestScenario {
  category: string;
  title: string;
  modes: string[];
  description: string;
  example: string;
  flow: string[];
  expected: string;
}

const TEST_SCENARIOS: TestScenario[] = [
  {
    category: "READ",
    title: "복약 현황 조회",
    modes: ["READ"],
    description:
      "오늘 복약 Snapshot 또는 복약 상태 조회가 실제 Backend 데이터를 읽고 text 응답으로 정리하는지 확인합니다.",
    example: "오늘 먹을 약과 복용 상태를 알려줘",
    flow: ["사용자 발화", "복약 상태 조회", "text 응답"],
    expected:
      "Backend의 오늘 복약 상태와 화면에 표시되는 약 이름·상태가 일치해야 합니다.",
  },
  {
    category: "WRITE",
    title: "복약 기록",
    modes: ["APPROVAL", "WRITE"],
    description:
      "복용 완료 발화가 바로 저장되지 않고, 사용자 승인 카드 이후 동기 쓰기로 기록되는지 확인합니다.",
    example: "방금 암로디핀 5mg 먹었어",
    flow: [
      "사용자 발화",
      "대상 복약 확인",
      "기록 승인",
      "Backend 동기 저장",
      "text 결과",
    ],
    expected:
      "승인 전에는 DB가 변경되지 않고, ‘기록’ 선택 후 해당 복약 건만 taken으로 변경되어야 합니다.",
  },
  {
    category: "MULTI WRITE",
    title: "여러 음식 기록",
    modes: ["MULTI", "APPROVAL", "WRITE"],
    description:
      "음식 목록을 한 번에 검색하고 각 음식 후보를 1/N 방식으로 순차 선택한 뒤, 한 번의 승인으로 전체 식사를 기록하는지 확인합니다.",
    example: "아침에 토스트랑 우유 먹었는데 기록해줘",
    flow: [
      "음식 목록 검색",
      "후보 선택 1/2",
      "후보 선택 2/2",
      "식사 기록 승인",
      "Backend 동기 저장",
    ],
    expected:
      "검색은 한 번만 실행되고, 모든 음식 선택이 끝난 후 전체 음식이 한 식사로 저장되어야 합니다.",
  },
  {
    category: "SURVEY + WRITE",
    title: "부작용 평가",
    modes: ["SURVEY", "APPROVAL", "WRITE"],
    description:
      "증상 인식 후 PRO-CTCAE 원문 문항을 끝까지 순차 제시하고, 완료된 응답을 포함한 승인 카드 뒤에만 부작용 평가를 저장하는지 확인합니다.",
    example: "어제 약을 먹고 속이 메스꺼웠어",
    flow: [
      "증상 평가",
      "설문 1/2",
      "설문 2/2",
      "부작용 기록 승인",
      "Backend 동기 저장",
    ],
    expected:
      "모든 필수 문항에 답하기 전에는 기록 승인으로 넘어가지 않고, Excel 원문 문항·응답이 그대로 보존되어야 합니다.",
  },
  {
    category: "POLICY WRITE",
    title: "알림 정책 변경",
    modes: ["HIGH RISK", "APPROVAL", "WRITE"],
    description:
      "공개 policy_id를 조회한 뒤 변경안을 제안하고, 사용자가 승인해야만 Backend 정책 변경 API를 호출하는지 확인합니다.",
    example: "미복용 알림을 복약 예정 120분 후로 바꿔줘",
    flow: [
      "현재 정책 조회",
      "변경안 생성",
      "정책 승인",
      "Backend 동기 반영",
      "변경 결과 조회",
    ],
    expected:
      "승인 전 정책은 유지되고, 승인 후 같은 policy_id의 version이 증가하며 변경값이 다시 조회되어야 합니다.",
  },
];

function TestScenarioGuide({
  disabled,
  onFillExample,
}: {
  disabled: boolean;
  onFillExample: (example: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const [activeIndex, setActiveIndex] = useState(0);
  const [completed, setCompleted] = useState<Set<number>>(
    () => new Set(),
  );
  const current = TEST_SCENARIOS[activeIndex];
  const currentCompleted = completed.has(activeIndex);
  const progress = (completed.size / TEST_SCENARIOS.length) * 100;

  function toggleComplete() {
    setCompleted((previous) => {
      const next = new Set(previous);
      if (next.has(activeIndex)) {
        next.delete(activeIndex);
      } else {
        next.add(activeIndex);
      }
      return next;
    });
  }

  return (
    <details
      className="test-scenario-guide"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="test-scenario-heading">
          <strong>테스트 시나리오</strong>
        </span>
        <span
          className="test-scenario-progress"
          aria-label={`${TEST_SCENARIOS.length}개 중 ${completed.size}개 완료`}
        >
          <span>
            <b>{completed.size}</b>/{TEST_SCENARIOS.length} 완료
          </span>
          <span className="test-scenario-progress-track" aria-hidden="true">
            <span style={{ width: `${progress}%` }} />
          </span>
        </span>
        <span className="test-scenario-chevron" aria-hidden="true" />
      </summary>

      <div className="test-scenario-body">
        <nav
          className="test-scenario-tabs"
          aria-label="테스트 시나리오 항목"
        >
          {TEST_SCENARIOS.map((scenario, index) => (
            <button
              className={`test-scenario-tab${
                index === activeIndex ? " is-active" : ""
              }${completed.has(index) ? " is-complete" : ""}`}
              type="button"
              key={scenario.title}
              aria-current={index === activeIndex ? "true" : undefined}
              onClick={() => setActiveIndex(index)}
            >
              <small>{scenario.category}</small>
              <strong>{scenario.title}</strong>
            </button>
          ))}
        </nav>

        <div className="test-scenario-detail">
          <section className="test-scenario-brief">
            <div className="test-scenario-brief-head">
              <h3>{current.title}</h3>
              <div
                className="test-scenario-mode-badges"
                aria-label="테스트 유형"
              >
                {current.modes.map((mode) => (
                  <span key={mode}>{mode}</span>
                ))}
              </div>
            </div>
            <p className="test-scenario-description">
              {current.description}
            </p>
            <div className="test-scenario-example">
              <span>예시 발화</span>
              <b>{current.example}</b>
            </div>
            <div
              className="test-scenario-flow"
              aria-label="예상 처리 순서"
            >
              {current.flow.map((step, index) => (
                <span className="test-scenario-flow-fragment" key={step}>
                  <span className="test-scenario-flow-step">{step}</span>
                  {index < current.flow.length - 1 ? (
                    <span
                      className="test-scenario-flow-arrow"
                      aria-hidden="true"
                    >
                      →
                    </span>
                  ) : null}
                </span>
              ))}
            </div>
          </section>

          <aside className="test-scenario-criteria">
            <p>
              <strong>통과 기준</strong>
              <span>{current.expected}</span>
            </p>
          </aside>
        </div>

        <div className="test-scenario-actions">
          <button
            className="compact-button ghost"
            type="button"
            disabled={disabled}
            onClick={() => onFillExample(current.example)}
          >
            예시 문장 입력
          </button>
          <button
            className={`compact-button${
              currentCompleted ? " is-complete" : ""
            }`}
            type="button"
            onClick={toggleComplete}
          >
            {currentCompleted ? "완료 표시 취소" : "완료로 표시"}
          </button>
        </div>
      </div>
    </details>
  );
}

function isPendingStructured(
  message: ClientChatHistoryMessage,
): boolean {
  return (
    message.role === "assistant" &&
    (message.message_type === "selection_box" ||
      message.message_type === "input_box") &&
    message.processing_status === "pending"
  );
}

function MessageTables({
  tables,
}: {
  tables: NonNullable<
    NonNullable<ClientChatHistoryMessage["content"]>["tables"]
  >;
}) {
  return (
    <div className="contract-message-tables">
      {tables.map((table, tableIndex) => (
        <section
          className="contract-table"
          key={`${table.table_title ?? "table"}-${tableIndex}`}
        >
          {table.table_title ? <h4>{table.table_title}</h4> : null}
          <dl>
            {table.rows.map((row, rowIndex) => (
              <div key={`${row.column}-${rowIndex}`}>
                <dt>{row.column}</dt>
                <dd>{row.value}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </div>
  );
}

function assistantMessageText(
  message: ClientChatHistoryMessage,
): string {
  const content = message.content;
  const parts: string[] = [];
  if (content?.message_title) {
    parts.push(content.message_title);
  }
  if (content?.text) {
    parts.push(content.text);
  } else if (message.message) {
    parts.push(message.message);
  }
  content?.tables?.forEach((table) => {
    if (table.table_title) {
      parts.push(table.table_title);
    }
    table.rows.forEach((row) => {
      parts.push(`${row.column}: ${row.value}`);
    });
  });
  if (content?.selections?.length) {
    parts.push(`선택 항목: ${content.selections.join(", ")}`);
  }
  content?.inputs?.forEach((input) => {
    parts.push(`${input.label}${input.options.unit ? ` (${input.options.unit})` : ""}`);
  });
  return parts.join("\n").trim();
}

function IconGlyph({
  kind,
}: {
  kind: "like" | "dislike" | "copy";
}) {
  if (kind === "copy") {
    return <span className="copy-action-icon" aria-hidden="true" />;
  }
  return (
    <span className="reaction-action-icon" aria-hidden="true">
      {kind === "like" ? "👍" : "👎"}
    </span>
  );
}

async function copyMessageText(
  text: string,
  {
    emptyMessage,
    successMessage,
    failureMessage,
  }: {
    emptyMessage: string;
    successMessage: string;
    failureMessage: string;
  },
  showToast: ChatPageProps["showToast"],
) {
  if (!text.trim()) {
    showToast(emptyMessage, "warning");
    return;
  }
  try {
    if (!navigator.clipboard) {
      throw new Error("Clipboard API unavailable");
    }
    await navigator.clipboard.writeText(text);
    showToast(successMessage, "neutral");
  } catch {
    showToast(failureMessage, "warning");
  }
}

function FeedbackControls({
  message,
  initiallySent,
  onFeedback,
  onReaction,
  showToast,
}: {
  message: ClientChatHistoryMessage;
  initiallySent: boolean;
  onFeedback: (assistantMessageId: string, opinion: string) => Promise<void>;
  onReaction: ChatPageProps["onReaction"];
  showToast: ChatPageProps["showToast"];
}) {
  const feedbackFormId = useId();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [open, setOpen] = useState(false);
  const [opinion, setOpinion] = useState("");
  const [sent, setSent] = useState(initiallySent);
  const [sending, setSending] = useState(false);
  const [reaction, setReaction] = useState<FeedbackReaction | null>(
    message.reaction ?? null,
  );
  const [reactionSending, setReactionSending] = useState(false);

  useEffect(() => {
    setReaction(message.reaction ?? null);
  }, [message.reaction]);

  useEffect(() => {
    setSent(initiallySent);
  }, [initiallySent]);

  useEffect(() => {
    if (open) {
      textareaRef.current?.focus();
    }
  }, [open]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = opinion.trim();
    if (!normalized) {
      showToast("의견을 입력해 주세요.", "warning");
      textareaRef.current?.focus();
      return;
    }

    setSending(true);
    try {
      await onFeedback(message.message_id, normalized);
      setSent(true);
      setOpen(false);
      setOpinion("");
    } catch {
      // Keep the form and text intact so the opinion can be retried.
    } finally {
      setSending(false);
    }
  }

  async function toggleReaction(next: FeedbackReaction) {
    if (reactionSending) {
      return;
    }
    const previous = reaction;
    if (previous === next) {
      return;
    }
    setReaction(next);
    setReactionSending(true);
    try {
      setReaction(await onReaction(message.message_id, next));
    } catch {
      setReaction(previous);
    } finally {
      setReactionSending(false);
    }
  }

  async function copyAnswer() {
    await copyMessageText(
      assistantMessageText(message),
      {
        emptyMessage: "복사할 답변 내용이 없습니다.",
        successMessage: "답변을 복사했습니다.",
        failureMessage: "답변을 복사하지 못했습니다.",
      },
      showToast,
    );
  }

  return (
    <>
      <div
        className="message-feedback message-actions"
        role="group"
        aria-label="AI 답변 작업"
      >
        <button
          className={`feedback-link icon-action-button reaction-button${
            reaction === "like" ? " is-active" : ""
          }`}
          type="button"
          aria-label="좋아요"
          aria-pressed={reaction === "like"}
          title="좋아요"
          disabled={reactionSending}
          onClick={() => void toggleReaction("like")}
        >
          <IconGlyph kind="like" />
        </button>
        <button
          className={`feedback-link icon-action-button reaction-button${
            reaction === "dislike" ? " is-active" : ""
          }`}
          type="button"
          aria-label="싫어요"
          aria-pressed={reaction === "dislike"}
          title="싫어요"
          disabled={reactionSending}
          onClick={() => void toggleReaction("dislike")}
        >
          <IconGlyph kind="dislike" />
        </button>
        <button
          className="feedback-link"
          type="button"
          aria-expanded={open}
          aria-controls={feedbackFormId}
          onClick={() => setOpen((value) => !value)}
        >
          의견 남기기
        </button>
        <button
          className="feedback-link icon-action-button"
          type="button"
          aria-label="복사하기"
          title="복사하기"
          onClick={() => void copyAnswer()}
        >
          <IconGlyph kind="copy" />
        </button>
        <span className="feedback-status" aria-live="polite">
          {sent ? "의견 전송됨" : ""}
        </span>
      </div>
      <form
        id={feedbackFormId}
        className="inline-feedback-form"
        hidden={!open}
        onSubmit={(event) => void submit(event)}
      >
        <label>
          <span>
            이 답변에 대한 의견을 알려주세요 <small>선택 입력</small>
          </span>
          <textarea
            ref={textareaRef}
            maxLength={4000}
            rows={3}
            placeholder="답변에서 좋았던 점이나 개선이 필요한 내용을 적어주세요."
            value={opinion}
            onChange={(event) => setOpinion(event.target.value)}
          />
        </label>
        <div className="feedback-form-footer">
          <span className="character-count">
            {opinion.length.toLocaleString("ko-KR")} / 4,000
          </span>
          <div>
            <button
              className="compact-button ghost"
              type="button"
              disabled={sending}
              onClick={() => setOpen(false)}
            >
              취소
            </button>
            <button
              className="compact-button"
              type="submit"
              disabled={sending}
            >
              의견 보내기
            </button>
          </div>
        </div>
      </form>
    </>
  );
}

function StructuredSelection({
  message,
  submittedValue,
  disabled,
  onReply,
}: {
  message: ClientChatHistoryMessage;
  submittedValue: string | null;
  disabled: boolean;
  onReply: ChatPageProps["onSend"];
}) {
  const content = message.content;
  const answered = message.processing_status !== "pending";
  const [selected, setSelected] = useState<string | null>(
    answered ? submittedValue : null,
  );

  useEffect(() => {
    if (answered && submittedValue) {
      setSelected(submittedValue);
    }
  }, [answered, submittedValue]);

  async function choose(value: string) {
    if (disabled || answered) {
      return;
    }
    try {
      await onReply(value, "selection_box", message.message_id);
      setSelected(value);
    } catch {
      // The card remains pending and retryable.
    }
  }

  if (!content?.selections?.length) {
    return null;
  }

  return (
    <div className="contract-message-selections result-selection">
      <div className="selection-buttons">
        {content.selections.map((selection) => (
          <button
            className={selected === selection ? "is-selected" : ""}
            type="button"
            disabled={disabled || answered || selected !== null}
            key={selection}
            onClick={() => void choose(selection)}
          >
            {selection}
          </button>
        ))}
      </div>
      {selected ? (
        <p className="selection-result">{selected}으로 응답했습니다.</p>
      ) : null}
    </div>
  );
}

function initialInputValue(input: ChatInput): string {
  if (input.value !== null && input.value !== undefined) {
    return String(input.value);
  }
  if (input.type === "dropdown") {
    return input.options.selections?.[0] ?? "";
  }
  return "";
}

function parseInputResponse(
  value: string,
  inputs: ChatInput[],
): Record<string, string> | null {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      !Array.isArray(parsed)
    ) {
      const record = parsed as Record<string, unknown>;
      return Object.fromEntries(
        Object.entries(record).map(([label, inputValue]) => [
          label,
          String(inputValue ?? ""),
        ]),
      );
    }
  } catch {
    // Local optimistic messages are already formatted for display.
  }

  const entries = value
    .split(" · ")
    .map((entry) => {
      const separator = entry.indexOf(":");
      if (separator < 0) {
        return null;
      }
      return [
        entry.slice(0, separator).trim(),
        entry.slice(separator + 1).trim(),
      ] as const;
    })
    .filter((entry): entry is readonly [string, string] => entry !== null);

  if (!entries.length || entries.length > inputs.length) {
    return null;
  }
  return Object.fromEntries(entries);
}

function formatInputResponse(value: string, inputs: ChatInput[]): string {
  const parsed = parseInputResponse(value, inputs);
  if (!parsed) {
    return value;
  }

  const inputLabels = inputs.map((input) => input.label);
  const orderedLabels = [
    ...inputLabels.filter((label) => label in parsed),
    ...Object.keys(parsed).filter((label) => !inputLabels.includes(label)),
  ];
  return orderedLabels
    .map((label) => `${label}: ${parsed[label]}`)
    .join(" · ");
}

function StructuredInputs({
  message,
  submittedValue,
  disabled,
  onReply,
}: {
  message: ClientChatHistoryMessage;
  submittedValue: string | null;
  disabled: boolean;
  onReply: ChatPageProps["onSend"];
}) {
  const inputs = useMemo(
    () => message.content?.inputs ?? [],
    [message.content?.inputs],
  );
  const answered = message.processing_status !== "pending";
  const [values, setValues] = useState<Record<string, string>>(() => {
    const defaults = Object.fromEntries(
      inputs.map((input) => [input.label, initialInputValue(input)]),
    );
    const submitted =
      answered && submittedValue
        ? parseInputResponse(submittedValue, inputs)
        : null;
    return submitted ? { ...defaults, ...submitted } : defaults;
  });
  const [submittedText, setSubmittedText] = useState(
    answered && submittedValue
      ? formatInputResponse(submittedValue, inputs)
      : "",
  );

  useEffect(() => {
    if (!answered || !submittedValue) {
      return;
    }
    const submitted = parseInputResponse(submittedValue, inputs);
    if (submitted) {
      setValues((current) => ({ ...current, ...submitted }));
    }
    setSubmittedText(formatInputResponse(submittedValue, inputs));
  }, [answered, inputs, submittedValue]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || answered) {
      return;
    }
    try {
      await onReply(JSON.stringify(values), "input_box", message.message_id);
      setSubmittedText(
        Object.entries(values)
          .map(([label, value]) => `${label}: ${value}`)
          .join(" · "),
      );
    } catch {
      // Preserve entered values and keep the structured response active.
    }
  }

  if (!inputs.length) {
    return null;
  }

  return (
    <form
      className="contract-message-inputs result-input-form"
      onSubmit={(event) => void submit(event)}
    >
      <div className="result-input-grid">
        {inputs.map((input) => (
          <label key={input.label}>
            <span>{input.label}</span>
            {input.type === "dropdown" ? (
              <select
                name={input.label}
                required
                disabled={disabled || answered || Boolean(submittedText)}
                value={values[input.label] ?? ""}
                onChange={(event) =>
                  setValues((current) => ({
                    ...current,
                    [input.label]: event.target.value,
                  }))
                }
              >
                {input.options.selections?.map((selection) => (
                  <option key={selection}>{selection}</option>
                ))}
              </select>
            ) : (
              <span className="input-with-unit">
                <input
                  name={input.label}
                  type="number"
                  step="any"
                  min={input.options.lower ?? undefined}
                  max={input.options.upper ?? undefined}
                  required
                  disabled={disabled || answered || Boolean(submittedText)}
                  value={values[input.label] ?? ""}
                  onChange={(event) =>
                    setValues((current) => ({
                      ...current,
                      [input.label]: event.target.value,
                    }))
                  }
                />
                <span>{input.options.unit}</span>
              </span>
            )}
          </label>
        ))}
      </div>
      <div className="result-submit-row">
        <button
          className="compact-button"
          type="submit"
          disabled={disabled || answered || Boolean(submittedText)}
        >
          입력값 보내기
        </button>
        <span className="input-result-status" role="status">
          {submittedText}
        </span>
      </div>
    </form>
  );
}

function ChatMessage({
  message,
  submittedValue,
  userDisplayMessage,
  chatSending,
  onReply,
  onRetry,
  onFeedback,
  onReaction,
  showToast,
}: {
  message: ClientChatHistoryMessage;
  submittedValue: string | null;
  userDisplayMessage: string | null;
  chatSending: boolean;
  onReply: ChatPageProps["onSend"];
  onRetry: ChatPageProps["onRetry"];
  onFeedback: ChatPageProps["onFeedback"];
  onReaction: ChatPageProps["onReaction"];
  showToast: ChatPageProps["showToast"];
}) {
  if (message.role === "user") {
    const deliveryStatus = message.delivery_status;
    const displayMessage = userDisplayMessage ?? message.message ?? "";

    async function copyUserMessage() {
      await copyMessageText(
        displayMessage,
        {
          emptyMessage: "복사할 메시지 내용이 없습니다.",
          successMessage: "메시지를 복사했습니다.",
          failureMessage: "메시지를 복사하지 못했습니다.",
        },
        showToast,
      );
    }

    return (
      <article
        className={`chat-message user${
          deliveryStatus ? ` is-${deliveryStatus}` : ""
        }`}
        data-message-id={message.message_id}
        data-delivery-status={deliveryStatus ?? "sent"}
      >
        <div className="message-column">
          <div className="message-meta user-meta">
            <span>
              {formatKoreanMessageTime(message.created_at)}
              {deliveryStatus === "sending" ? " · 전송 중" : ""}
              {deliveryStatus === "failed" ? " · 전송 실패" : ""}
            </span>
            <strong>나</strong>
          </div>
          <div className="message-bubble">
            <p>{displayMessage}</p>
          </div>
          <div
            className="message-feedback message-actions user-message-actions"
            role="group"
            aria-label="내 메시지 작업"
          >
            <button
              className="feedback-link icon-action-button"
              type="button"
              aria-label="복사하기"
              title="복사하기"
              onClick={() => void copyUserMessage()}
            >
              <IconGlyph kind="copy" />
            </button>
          </div>
          {deliveryStatus === "failed" ? (
            <div className="message-delivery-error" role="alert">
              <span>{message.delivery_error ?? "메시지를 보내지 못했습니다."}</span>
              {message.delivery_retryable ? (
                <button
                  className="feedback-link"
                  type="button"
                  disabled={chatSending}
                  onClick={() =>
                    void onRetry(message.message_id).catch(
                      () => undefined,
                    )
                  }
                >
                  다시 시도
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      </article>
    );
  }

  const content = message.content;
  return (
    <article
      className={`chat-message assistant${
        message.processing_status === "streaming" ? " is-streaming" : ""
      }`}
      data-message-id={message.message_id}
      data-message-type={message.message_type ?? "text"}
      aria-busy={message.processing_status === "streaming"}
      aria-live={
        message.processing_status === "streaming" ? "off" : undefined
      }
    >
      <div className="avatar ai-avatar" aria-hidden="true">
        AI
      </div>
      <div className="message-column">
        <div className="message-meta">
          <strong>닥터앤서 AI</strong>
          <span>{formatKoreanMessageTime(message.created_at)}</span>
        </div>
        <div className="message-bubble">
          <div className="contract-message">
            {content?.message_title ? (
              <h3 className="contract-message-title">
                {content.message_title}
              </h3>
            ) : null}
            {content?.text ? (
              <div className="contract-message-text">
                <AssistantMarkdown text={content.text} />
              </div>
            ) : message.message ? (
              <div className="contract-message-text">
                <AssistantMarkdown text={message.message} />
              </div>
            ) : null}
            {content?.tables?.length ? (
              <MessageTables tables={content.tables} />
            ) : null}
            {message.message_type === "selection_box" ? (
              <StructuredSelection
                message={message}
                submittedValue={submittedValue}
                disabled={chatSending}
                onReply={onReply}
              />
            ) : null}
            {message.message_type === "input_box" ? (
              <StructuredInputs
                message={message}
                submittedValue={submittedValue}
                disabled={chatSending}
                onReply={onReply}
              />
            ) : null}
          </div>
        </div>
        {message.processing_status === "streaming" ? null : (
          <FeedbackControls
            message={message}
            initiallySent={message.opinion_submitted ?? false}
            onFeedback={onFeedback}
            onReaction={onReaction}
            showToast={showToast}
          />
        )}
      </div>
    </article>
  );
}

export default function ChatPage({
  active,
  days,
  historyLoading,
  chatSending,
  chatError,
  chatErrorRetryable,
  draft,
  onDraftChange,
  onLoadPrevious,
  onSend,
  onRetry,
  onFeedback,
  onReaction,
  showToast,
}: ChatPageProps) {
  const logRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const lastScrollTop = useRef(0);
  const loadingPrevious = useRef(false);
  const wasChatSending = useRef(chatSending);
  const newestMessage = days.at(-1)?.messages.at(-1);
  const newestClientMessageId =
    newestMessage?.delivery_status === "sending"
      ? newestMessage.message_id
      : null;
  const streamingAssistantText = useMemo(
    () =>
      days
        .flatMap((day) => day.messages)
        .filter(
          (message) =>
            message.role === "assistant" &&
            message.processing_status === "streaming",
        )
        .at(-1)?.content?.text ?? "",
    [days],
  );

  const pendingStructured = useMemo(
    () =>
      [...days]
        .reverse()
        .flatMap((day) => [...day.messages].reverse())
        .find(isPendingStructured) ?? null,
    [days],
  );
  const structuredResponseViews = useMemo(() => {
    const submittedByAssistant = new Map<string, string>();
    const displayByUser = new Map<string, string>();
    const messages = days.flatMap((day) => day.messages);
    const messagesById = new Map(
      messages.map((message) => [message.message_id, message]),
    );

    messages.forEach((message) => {
      if (
        message.role !== "assistant" ||
        (message.message_type !== "selection_box" &&
          message.message_type !== "input_box") ||
        message.processing_status !== "answered" ||
        !message.response_message_id
      ) {
        return;
      }

      const response = messagesById.get(message.response_message_id);
      if (
        response?.role !== "user" ||
        response.source_message_id !== message.message_id ||
        !response.message
      ) {
        return;
      }

      submittedByAssistant.set(message.message_id, response.message);
      if (message.message_type === "input_box") {
        displayByUser.set(
          response.message_id,
          formatInputResponse(response.message, message.content?.inputs ?? []),
        );
      }
    });

    return { submittedByAssistant, displayByUser };
  }, [days]);

  useEffect(() => {
    document.body.dataset.activeTab = active ? "chat" : "home";
    if (active) {
      requestAnimationFrame(() => {
        const log = logRef.current;
        if (!log) {
          return;
        }
        log.scrollTop = log.scrollHeight;
        lastScrollTop.current = log.scrollTop;
      });
    }
  }, [active]);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) {
      return;
    }
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
  }, [draft]);

  useEffect(() => {
    const sendCompleted = wasChatSending.current && !chatSending;
    wasChatSending.current = chatSending;
    if (sendCompleted) {
      requestAnimationFrame(() => {
        const log = logRef.current;
        if (log) {
          log.scrollTop = log.scrollHeight;
          lastScrollTop.current = log.scrollTop;
        }
      });
    }
  }, [chatSending]);

  useEffect(() => {
    if (!active || !newestClientMessageId) {
      return;
    }
    requestAnimationFrame(() => {
      const log = logRef.current;
      if (log) {
        log.scrollTop = log.scrollHeight;
        lastScrollTop.current = log.scrollTop;
      }
    });
  }, [active, newestClientMessageId]);

  useEffect(() => {
    if (!active || !streamingAssistantText) {
      return;
    }
    requestAnimationFrame(() => {
      const log = logRef.current;
      if (!log) {
        return;
      }
      const distanceFromBottom =
        log.scrollHeight - log.scrollTop - log.clientHeight;
      if (distanceFromBottom <= 120) {
        log.scrollTop = log.scrollHeight;
        lastScrollTop.current = log.scrollTop;
      }
    });
  }, [active, streamingAssistantText]);

  async function loadPrevious() {
    const log = logRef.current;
    if (!log || loadingPrevious.current) {
      return;
    }
    loadingPrevious.current = true;
    const previousHeight = log.scrollHeight;
    const previousScrollTop = log.scrollTop;
    try {
      await onLoadPrevious();
      requestAnimationFrame(() => {
        log.scrollTop =
          log.scrollHeight - previousHeight + previousScrollTop;
        lastScrollTop.current = log.scrollTop;
      });
    } catch {
      // Preserve the current history when the read fails.
    } finally {
      loadingPrevious.current = false;
    }
  }

  function handleScroll() {
    const log = logRef.current;
    if (!log) {
      return;
    }
    const movingUp = log.scrollTop < lastScrollTop.current;
    lastScrollTop.current = log.scrollTop;
    if (active && movingUp && log.scrollTop <= 8) {
      void loadPrevious();
    }
  }

  function handleWheel(event: React.WheelEvent<HTMLDivElement>) {
    if (active && event.deltaY < 0 && (logRef.current?.scrollTop ?? 1) <= 8) {
      void loadPrevious();
    }
  }

  async function submitComposer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || pendingStructured || chatSending) {
      return;
    }
    try {
      await onSend(message, "text");
    } catch {
      // Keep the draft intact for retry.
    }
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  return (
    <section className="panel chat-panel">
      <div className="panel-header chat-panel-header">
        <div>
          <p className="panel-kicker">AI CHAT</p>
          <h2>에이전트와의 대화</h2>
        </div>
      </div>

      <TestScenarioGuide
        disabled={Boolean(pendingStructured) || chatSending}
        onFillExample={(example) => {
          onDraftChange(example);
          requestAnimationFrame(() => inputRef.current?.focus());
        }}
      />

      <div className="chat-layout">
        <div className="conversation-shell">
          <div
            ref={logRef}
            id="chat-log"
            className="chat-log"
            role="log"
            aria-live="polite"
            aria-label="대화 내용"
            onScroll={handleScroll}
            onWheel={handleWheel}
          >
            <div
              className="history-load-status"
              role="status"
              hidden={!historyLoading}
            >
              <span className="history-loading-dot" aria-hidden="true" />
              이전 대화를 불러오는 중입니다.
            </div>

            {days.map((day) => (
              <section key={day.date}>
                <div
                  className="date-divider"
                  data-conversation-date={day.date}
                >
                  <time dateTime={day.date}>
                    {formatKoreanCalendarDate(day.date)}
                  </time>
                </div>
                {day.messages.map((message) => (
                  <ChatMessage
                    key={message.message_id}
                    message={message}
                    submittedValue={
                      structuredResponseViews.submittedByAssistant.get(
                        message.message_id,
                      ) ?? null
                    }
                    userDisplayMessage={
                      structuredResponseViews.displayByUser.get(
                        message.message_id,
                      ) ?? null
                    }
                    chatSending={chatSending}
                    onReply={onSend}
                    onRetry={onRetry}
                    onFeedback={onFeedback}
                    onReaction={onReaction}
                    showToast={showToast}
                  />
                ))}
              </section>
            ))}

            {!days.some((day) => day.messages.length) &&
            !historyLoading &&
            !chatSending ? (
              <div className="chat-empty-state">
                <strong>아직 대화가 없습니다.</strong>
                <p>아래 입력창에서 AI 에이전트에게 질문해 주세요.</p>
              </div>
            ) : null}

            {chatSending && !streamingAssistantText ? (
              <article className="chat-message assistant chat-pending">
                <div className="avatar ai-avatar" aria-hidden="true">
                  AI
                </div>
                <div className="message-column">
                  <div className="message-meta">
                    <strong>닥터앤서 AI</strong>
                    <span>응답 생성 중</span>
                  </div>
                  <div className="message-bubble">
                    <span className="typing-dots" aria-hidden="true">
                      <span />
                      <span />
                      <span />
                    </span>
                    <span>처리중입니다</span>
                  </div>
                </div>
              </article>
            ) : null}
          </div>

          {chatError ? (
            <div className="chat-runtime-error" role="alert">
              <strong>AI 응답을 받지 못했습니다.</strong>
              <p>{chatError}</p>
              <span>
                {chatErrorRetryable
                  ? "실패한 메시지 버블의 다시 시도 버튼으로 같은 요청을 재전송할 수 있습니다."
                  : "요청 내용을 확인한 뒤 새 메시지로 다시 보내 주세요."}
              </span>
            </div>
          ) : null}

          <div className="quick-prompts" aria-label="빠른 질문">
            <span>빠른 질문</span>
            <button
              type="button"
              disabled={Boolean(pendingStructured) || chatSending}
              onClick={() => {
                onDraftChange("오늘 남은 복약 일정을 알려줘");
                inputRef.current?.focus();
              }}
            >
              오늘 남은 복약
            </button>
            <button
              type="button"
              disabled={Boolean(pendingStructured) || chatSending}
              onClick={() => {
                onDraftChange("메스꺼움과 약의 관련성을 설명해줘");
                inputRef.current?.focus();
              }}
            >
              부작용 문의
            </button>
            <button
              type="button"
              disabled={Boolean(pendingStructured) || chatSending}
              onClick={() => {
                onDraftChange("오늘 영양 상태를 요약해줘");
                inputRef.current?.focus();
              }}
            >
              영양 요약
            </button>
          </div>

          <form
            id="chat-form"
            className="chat-composer"
            onSubmit={(event) => void submitComposer(event)}
          >
            <label className="visually-hidden" htmlFor="chat-message-input">
              AI 에이전트에게 질문하기
            </label>
            <textarea
              ref={inputRef}
              id="chat-message-input"
              rows={1}
              maxLength={2000}
              placeholder="복약, 증상, 식사 기록에 대해 질문해 주세요."
              required
              disabled={Boolean(pendingStructured) || chatSending}
              value={draft}
              onChange={(event) => onDraftChange(event.target.value)}
              onKeyDown={handleComposerKeyDown}
            />
            <button
              type="submit"
              aria-label="메시지 보내기"
              disabled={Boolean(pendingStructured) || chatSending}
            >
              <span>보내기</span>
              <span aria-hidden="true">→</span>
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}

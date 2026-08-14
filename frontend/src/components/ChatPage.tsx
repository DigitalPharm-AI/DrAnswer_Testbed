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
  ClientResponseTiming,
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
  medicationSideEffectEnabled: boolean;
  days: ClientChatHistoryDay[];
  historyLoading: boolean;
  chatSending: boolean;
  chatError: string | null;
  chatErrorRetryable: boolean;
  activeResponseTiming: ClientResponseTiming | null;
  failedResponseTiming: ClientResponseTiming | null;
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

function formatStopwatch(milliseconds: number): string {
  const safeMilliseconds = Math.max(0, milliseconds);
  const minutes = Math.floor(safeMilliseconds / 60_000);
  const seconds = (safeMilliseconds % 60_000) / 1_000;
  return `${String(minutes).padStart(2, "0")}:${seconds
    .toFixed(1)
    .padStart(4, "0")}`;
}

function formatResponseSeconds(milliseconds: number): string {
  return `${(Math.max(0, milliseconds) / 1_000).toFixed(1)}초`;
}

function ResponseLatency({
  timing,
}: {
  timing: ClientResponseTiming;
}) {
  const [elapsedMs, setElapsedMs] = useState(() =>
    timing.totalResponseMs ??
    Math.max(0, performance.now() - timing.startedAtMonotonicMs),
  );

  useEffect(() => {
    if (timing.status !== "running") {
      setElapsedMs(timing.totalResponseMs ?? 0);
      return;
    }
    const updateElapsed = () => {
      setElapsedMs(
        Math.max(0, performance.now() - timing.startedAtMonotonicMs),
      );
    };
    updateElapsed();
    const intervalId = window.setInterval(updateElapsed, 100);
    return () => window.clearInterval(intervalId);
  }, [timing.startedAtMonotonicMs, timing.status, timing.totalResponseMs]);

  const shownMs = timing.totalResponseMs ?? elapsedMs;
  const hasFirstResponse = timing.firstResponseMs !== null;
  const generationMs = hasFirstResponse
    ? Math.max(0, shownMs - timing.firstResponseMs!)
    : null;
  const firstResponseLabel = hasFirstResponse
    ? `첫 응답 ${formatResponseSeconds(timing.firstResponseMs!)}`
    : timing.status === "failed"
      ? "첫 응답 없음"
      : `첫 응답 대기 ${formatStopwatch(shownMs)}`;
  const generationLabel = !hasFirstResponse
    ? timing.status === "failed"
      ? `실패 ${formatResponseSeconds(shownMs)}`
      : "응답 생성 대기"
    : timing.status === "failed"
      ? `응답 생성 실패 ${formatResponseSeconds(generationMs!)}`
      : timing.status === "running"
        ? `응답 생성 ${formatStopwatch(generationMs!)}`
        : `응답 생성 ${formatResponseSeconds(generationMs!)}`;
  const isSettled = timing.status !== "running";
  const totalResponseLabel = timing.status === "failed"
    ? `전체 실패 ${formatResponseSeconds(shownMs)}`
    : `전체 ${formatResponseSeconds(shownMs)}`;

  return (
    <span className="response-latency-wrap">
      <span
        className={`response-latency-pill is-${timing.status}`}
        tabIndex={0}
        aria-label={`${firstResponseLabel}. ${generationLabel}.${
          isSettled ? ` ${totalResponseLabel}.` : ""
        } 시뮬레이션 시간과 별도 측정`}
      >
        <span className="response-timer-dot" aria-hidden="true" />
        <span className="response-latency-segment">{firstResponseLabel}</span>
        <span className="response-latency-separator" aria-hidden="true">+</span>
        <span className="response-latency-segment">{generationLabel}</span>
        {isSettled ? (
          <>
            <span className="response-latency-separator" aria-hidden="true">=</span>
            <span className="response-latency-segment response-latency-total">
              {totalResponseLabel}
            </span>
          </>
        ) : null}
      </span>
      <span className="response-latency-tooltip" role="tooltip">
        <strong>실제 응답 시간</strong>
        <span>{firstResponseLabel}</span>
        <span>{generationLabel}</span>
        <span>{isSettled ? totalResponseLabel : "전체 완료 대기 중"}</span>
        <span>시뮬레이션 시간과 별도 측정</span>
      </span>
    </span>
  );
}

interface TestScenarioTurn {
  user: string;
  expected: string;
}

interface TestScenario {
  category: string;
  title: string;
  feature?: "medication_side_effect";
  modes: string[];
  description: string;
  example?: string;
  conversation?: TestScenarioTurn[];
  flow: string[];
  expected: string;
}

type TestScenarioLevel = "basic" | "advanced" | "conversation";

const BASIC_TEST_SCENARIOS: TestScenario[] = [
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
    feature: "medication_side_effect",
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

const ADVANCED_TEST_SCENARIOS: TestScenario[] = [
  {
    category: "MULTI SURVEY + WRITE",
    title: "부작용 다건 평가",
    feature: "medication_side_effect",
    modes: ["MULTI", "SURVEY", "APPROVAL", "WRITE"],
    description:
      "여러 약과 여러 증상을 한 번에 말했을 때 위험 신호를 우선 분류하고, 증상별 평가와 저장을 이어가는지 확인합니다.",
    example:
      "당뇨약 뒤 어지럽고 메스꺼웠고, 고혈압 뒤 발진이 생겼으며 숨도 조금 찼어",
    flow: [
      "발화 분해",
      "위험 신호 선별",
      "증상별 평가",
      "건별 저장 승인",
      "Backend 동기 저장",
    ],
    expected:
      "호흡곤란 같은 위험 신호를 먼저 안내하고, 증상·의심 약물 조합별로 사용자가 승인한 항목만 저장해야 합니다.",
  },
  {
    category: "BATCH MEDICATION WRITE",
    title: "한 채팅 다중 복약 기록",
    modes: ["MULTI", "APPROVAL", "WRITE"],
    description:
      "복용·미복용·예정 상태가 섞인 여러 복약 항목을 한 발화에서 정확히 분리해 처리하는지 확인합니다.",
    example:
      "아침 혈압약과 당뇨약은 8시에 먹었고 점심 위장약은 못 먹었고 저녁약은 아직이에요",
    flow: [
      "복약 항목 분해",
      "일정 매칭",
      "상태별 승인",
      "일괄 기록",
      "결과 요약",
    ],
    expected:
      "각 약이 올바른 일정과 연결되고 복용·미복용·미기록 상태가 뒤섞이거나 중복 저장되지 않아야 합니다.",
  },
  {
    category: "RANGE READ + SUMMARY",
    title: "기간별 복약 이행 요약",
    modes: ["RANGE READ", "SUMMARY"],
    description:
      "기간 조건을 해석해 복약 기록을 집계하고, 누락·지연 패턴을 근거와 함께 요약하는지 확인합니다.",
    example: "지난 4주간 복약률과 자주 놓친 요일·시간대를 요약해줘",
    flow: ["기간 확정", "기록 범위 조회", "지표 집계", "패턴 요약"],
    expected:
      "조회 기간과 전체 예정·복용·미복용·지연 건수가 Backend 원본과 일치하고, 근거 없는 원인을 추정하지 않아야 합니다.",
  },
  {
    category: "READ + REPLACE",
    title: "기존 식사 전체 교체",
    modes: ["READ", "HIGH RISK", "APPROVAL", "MULTI WRITE"],
    description:
      "기존 식사 내역을 먼저 보여준 뒤 여러 끼를 새 내용으로 교체하는 고위험 일괄 변경 흐름을 확인합니다.",
    example:
      "오늘 식사를 전부 보여주고 아침은 죽, 점심은 비빔밥, 저녁은 샐러드로 모두 바꿔줘",
    flow: [
      "기존 식사 조회",
      "교체안 구성",
      "전체 변경 승인",
      "일괄 반영",
      "영양 합계 조회",
    ],
    expected:
      "기존 기록과 변경안을 비교해 보여주고 승인 전에는 삭제·교체하지 않으며, 승인 후 세 끼와 영양 합계가 함께 갱신되어야 합니다.",
  },
  {
    category: "MULTI POLICY WRITE",
    title: "알림 정책 동시 변경",
    modes: ["HIGH RISK", "MULTI", "APPROVAL", "WRITE"],
    description:
      "서로 다른 두 알림 정책의 변경 값을 한 요청에서 독립적으로 해석하고 반영하는지 확인합니다.",
    example:
      "복약 예정 알림은 45분 전, 미복용 확인 대화는 2시간 후로 바꿔줘",
    flow: [
      "두 정책 조회",
      "변경값 분리",
      "변경안 승인",
      "정책별 반영",
      "결과 재조회",
    ],
    expected:
      "예정 알림과 미복용 확인 정책이 서로 덮어쓰지 않고 각각 요청한 값으로 변경되며, 변경 전·후가 모두 표시되어야 합니다.",
  },
  {
    category: "CROSS DOMAIN",
    title: "복합 요청 부분 승인",
    modes: ["READ", "SUMMARY", "PARTIAL APPROVAL", "WRITE"],
    description:
      "복약 조회, 식사 요약, 복약 정정이 섞인 요청에서 조회와 쓰기를 분리하고 일부 변경만 승인받는지 확인합니다.",
    example:
      "지난 7일 복약 누락과 식사를 같이 요약하고, 어제 저녁약만 복용으로 정정해줘",
    flow: [
      "요청 분해",
      "복약·식사 조회",
      "정정안 별도 제시",
      "부분 승인",
      "승인 항목만 저장",
    ],
    expected:
      "조회 결과는 즉시 제공하되 쓰기 작업은 별도 승인을 거치고, 승인한 복약 정정 외의 데이터는 변경하지 않아야 합니다.",
  },
  {
    category: "CORRECTION + IDEMPOTENCY",
    title: "기록 정정·중복 방지",
    modes: ["CORRECTION", "APPROVAL", "IDEMPOTENCY"],
    description:
      "직전 기록의 시간 정정과 동일 요청 재전송 상황에서 기존 레코드를 정확히 수정하고 중복 생성을 막는지 확인합니다.",
    example:
      "방금 기록한 아침약은 8시가 아니라 8시 30분이야. 같은 요청이 두 번 가도 한 번만 반영해줘",
    flow: [
      "기존 기록 식별",
      "정정 내용 승인",
      "멱등 처리",
      "최종 상태 조회",
    ],
    expected:
      "원래 기록이 정정된 한 건으로 남고, 요청이 재전송되어도 추가 레코드나 중복 이벤트가 생성되지 않아야 합니다.",
  },
];

const CONVERSATION_TEST_SCENARIOS: TestScenario[] = [
  {
    category: "REFERENCE + ELLIPSIS",
    title: "지시 대상·생략 이해",
    modes: ["MULTI TURN", "CONTEXT", "APPROVAL"],
    description:
      "직전 응답에 나온 복약 목록을 기준으로 ‘그중’, ‘나머지’처럼 생략된 대상을 올바르게 이어서 해석하는지 확인합니다.",
    conversation: [
      {
        user: "오늘 먹을 약과 복용 상태를 알려줘",
        expected:
          "오늘 복약 일정을 약 이름·시간·상태별로 조회해 보여줍니다.",
      },
      {
        user: "그중 아침 약은 8시에 먹었어",
        expected:
          "직전 목록의 아침 복약 항목을 대상으로 복약 기록 승인 단계를 제시합니다.",
      },
    ],
    flow: ["복약 목록 조회", "지시 대상 연결", "생략 대상 복원", "기록 승인"],
    expected:
      "직전 응답에 실제로 포함된 약만 참조하고, ‘그중 아침 약’이 가리키는 복약 항목 외의 약을 기록 대상으로 포함하지 않아야 합니다.",
  },
  {
    category: "CORRECTION",
    title: "사용자 정정 우선 반영",
    modes: ["MULTI TURN", "CORRECTION", "APPROVAL"],
    description:
      "사용자가 후속 발화에서 시간이나 대상을 바로잡으면 이전 해석을 폐기하고 최신 정정을 기준으로 처리하는지 확인합니다.",
    conversation: [
      {
        user: "아침 암로디핀은 8시에 먹었어",
        expected:
          "대상 복약과 08:00 복용 기록 내용을 확인하는 승인 단계를 제시합니다.",
      },
      {
        user: "아니, 8시 30분이야",
        expected:
          "기존 08:00 변경안을 폐기하고 복용 시간을 08:30으로 교체해 다시 확인합니다.",
      },
    ],
    flow: ["초기 기록안", "사용자 정정", "이전안 폐기", "최신안 재확인"],
    expected:
      "08:00 변경안은 더 이상 승인할 수 없어야 하며, 최신 정정값인 08:30을 반영한 새 승인 내용만 제시해야 합니다.",
  },
  {
    category: "TOPIC SWITCH",
    title: "주제 전환 후 복귀",
    modes: ["MULTI TURN", "CONTEXT", "CROSS DOMAIN"],
    description:
      "복약 대화 중 식사로 주제를 바꿨다가 다시 복약으로 돌아왔을 때 각 도메인의 맥락을 섞지 않는지 확인합니다.",
    conversation: [
      {
        user: "지난 3일 복약 상태를 요약해줘",
        expected:
          "지난 3일의 복약 기록을 조회해 누락과 시간대 패턴을 요약합니다.",
      },
      {
        user: "오늘 점심은 김밥이었어. 기록은 하지 마",
        expected:
          "식사 내용을 이해하되 사용자의 지시에 따라 식사 기록 쓰기를 실행하지 않습니다.",
      },
      {
        user: "아까 복약에서 가장 자주 놓친 시간대가 언제였지?",
        expected:
          "첫 번째 복약 요약의 범위와 결과로 돌아가 해당 시간대를 답합니다.",
      },
    ],
    flow: ["복약 요약", "식사로 전환", "쓰기 금지 유지", "복약 맥락 복귀"],
    expected:
      "식사 정보를 복약 데이터로 섞지 않고, ‘아까 복약’이 첫 번째 조회 결과를 가리킨다는 점을 유지해야 합니다.",
  },
  {
    category: "CLARIFICATION",
    title: "모호성 단계적 해소",
    modes: ["MULTI TURN", "CLARIFICATION", "APPROVAL"],
    description:
      "대상 약이 불명확한 요청을 추측해 기록하지 않고, 후속 답변을 누적해 정확한 복약 항목을 찾는지 확인합니다.",
    conversation: [
      {
        user: "오늘 그 약 먹었어",
        expected:
          "‘그 약’의 후보가 하나로 정해지지 않으면 기록하지 않고 약 이름을 질문합니다.",
      },
      {
        user: "혈압약이야",
        expected:
          "혈압약 일정이 여러 개라면 복용 시간 또는 구체적인 약 이름을 추가로 질문합니다.",
      },
      {
        user: "저녁에 먹는 암로디핀이야",
        expected:
          "저녁 암로디핀 일정 하나로 대상을 확정하고 복약 기록 승인 단계를 제시합니다.",
      },
    ],
    flow: ["모호한 요청", "약 종류 확인", "시간·약 이름 확인", "대상 확정", "기록 승인"],
    expected:
      "정보가 충분해지기 전에는 쓰기나 승인을 진행하지 않고, 세 발화의 정보를 합쳐 정확히 한 복약 항목만 선택해야 합니다.",
  },
];

const TEST_SCENARIOS: Record<TestScenarioLevel, TestScenario[]> = {
  basic: BASIC_TEST_SCENARIOS,
  advanced: ADVANCED_TEST_SCENARIOS,
  conversation: CONVERSATION_TEST_SCENARIOS,
};

function TestScenarioGuide({
  disabled,
  medicationSideEffectEnabled,
  onFillExample,
}: {
  disabled: boolean;
  medicationSideEffectEnabled: boolean;
  onFillExample: (example: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const [level, setLevel] = useState<TestScenarioLevel>("basic");
  const [activeIndexes, setActiveIndexes] = useState<
    Record<TestScenarioLevel, number>
  >({ basic: 0, advanced: 0, conversation: 0 });
  const [completedByLevel, setCompletedByLevel] = useState<
    Record<TestScenarioLevel, Set<number>>
  >(() => ({
    basic: new Set(),
    advanced: new Set(),
    conversation: new Set(),
  }));
  const [nextConversationTurns, setNextConversationTurns] = useState<
    Record<number, number>
  >({});
  const scenarios = TEST_SCENARIOS[level].filter(
    (scenario) =>
      medicationSideEffectEnabled ||
      scenario.feature !== "medication_side_effect",
  );
  const activeIndex = Math.min(activeIndexes[level], scenarios.length - 1);
  const completed = completedByLevel[level];
  const current = scenarios[activeIndex];
  const currentCompleted = completed.has(activeIndex);
  const progress = (completed.size / scenarios.length) * 100;
  const levelLabel =
    level === "basic" ? "기본" : level === "advanced" ? "심화" : "대화";
  const conversationTurns = current.conversation ?? [];
  const nextConversationTurn = nextConversationTurns[activeIndex] ?? 0;
  const exampleButtonLabel = conversationTurns.length
    ? `${nextConversationTurn + 1}번 발화 입력`
    : "예시 문장 입력";

  function toggleComplete() {
    setCompletedByLevel((previous) => {
      const next = new Set(previous[level]);
      if (next.has(activeIndex)) {
        next.delete(activeIndex);
      } else {
        next.add(activeIndex);
      }
      return { ...previous, [level]: next };
    });
  }

  function fillCurrentExample() {
    if (conversationTurns.length) {
      onFillExample(conversationTurns[nextConversationTurn].user);
      setNextConversationTurns((previous) => ({
        ...previous,
        [activeIndex]: (nextConversationTurn + 1) % conversationTurns.length,
      }));
      return;
    }
    if (current.example) {
      onFillExample(current.example);
    }
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
          className="test-scenario-level-toggle"
          role="group"
          aria-label="시나리오 난이도"
        >
          {(
            [
              ["basic", "기본"],
              ["advanced", "심화"],
              ["conversation", "대화"],
            ] as const
          ).map(([value, label]) => (
            <button
              type="button"
              key={value}
              aria-pressed={level === value}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                setLevel(value);
              }}
            >
              {label}
            </button>
          ))}
        </span>
        <span
          className="test-scenario-progress"
          aria-label={`${levelLabel} 시나리오 ${scenarios.length}개 중 ${completed.size}개 완료`}
        >
          <span>
            <b>{completed.size}</b>/{scenarios.length} 완료
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
          aria-label={`${levelLabel} 테스트 시나리오 항목`}
        >
          {scenarios.map((scenario, index) => (
            <button
              className={`test-scenario-tab${
                index === activeIndex ? " is-active" : ""
              }${completed.has(index) ? " is-complete" : ""}`}
              type="button"
              key={scenario.title}
              aria-current={index === activeIndex ? "true" : undefined}
              onClick={() =>
                setActiveIndexes((previous) => ({
                  ...previous,
                  [level]: index,
                }))
              }
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
            {conversationTurns.length ? (
              <div
                className="test-scenario-conversation"
                aria-label="멀티턴 예시 대화"
              >
                <div className="test-scenario-conversation-head">
                  <span>멀티턴 예시</span>
                  <small>AI 응답을 확인한 뒤 다음 발화를 입력하세요.</small>
                </div>
                <ol>
                  {conversationTurns.map((turn, index) => (
                    <li
                      className={
                        index === nextConversationTurn ? "is-next" : undefined
                      }
                      key={`${index}-${turn.user}`}
                    >
                      <div className="test-scenario-turn-user">
                        <span>사용자 {index + 1}</span>
                        <b>{turn.user}</b>
                      </div>
                      <div className="test-scenario-turn-expected">
                        <span>기대 응답</span>
                        <p>{turn.expected}</p>
                      </div>
                    </li>
                  ))}
                </ol>
              </div>
            ) : (
              <div className="test-scenario-example">
                <span>예시 발화</span>
                <b>{current.example}</b>
              </div>
            )}
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
            disabled={
              disabled || (!current.example && conversationTurns.length === 0)
            }
            onClick={fillCurrentExample}
          >
            {exampleButtonLabel}
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

function numericInputAdjustmentStep(input: ChatInput): number | "any" {
  if (
    input.type !== "number" ||
    input.value === null ||
    input.value === undefined
  ) {
    return "any";
  }
  const defaultValue = Number(input.value);
  if (!Number.isFinite(defaultValue) || defaultValue === 0) {
    return "any";
  }
  return 10 ** (Math.floor(Math.log10(Math.abs(defaultValue))) - 1);
}

function formatAdjustmentNumber(value: number): string {
  return new Intl.NumberFormat("ko-KR", {
    maximumFractionDigits: 10,
  }).format(value);
}

function numericInputAdjustmentHint(input: ChatInput): string {
  const unit = input.options.unit ?? "";
  const step = numericInputAdjustmentStep(input);
  if (input.value === null || input.value === undefined || step === "any") {
    return unit ? `입력 단위 ${unit}` : "";
  }
  const defaultValue = Number(input.value);
  return (
    `기준 제공량 ${formatAdjustmentNumber(defaultValue)}${unit}` +
    ` · ${formatAdjustmentNumber(step)}${unit} 단위 조정`
  );
}

function updateNumericInputRangeValidity(
  element: HTMLInputElement,
  input: ChatInput,
): void {
  const value = element.valueAsNumber;
  const unit = input.options.unit ?? "";
  if (
    element.value &&
    Number.isFinite(value) &&
    input.options.lower !== null &&
    value < input.options.lower
  ) {
    element.setCustomValidity(
      `${formatAdjustmentNumber(input.options.lower)}${unit} 이상 입력해 주세요.`,
    );
    return;
  }
  element.setCustomValidity("");
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
        {inputs.map((input, inputIndex) => {
          const inputId = `${message.message_id}-input-${inputIndex}`;
          const hintId = `${inputId}-hint`;
          return (
            <div className="result-input-field" key={input.label}>
              <label htmlFor={inputId}>{input.label}</label>
              {input.type === "dropdown" ? (
                <select
                  id={inputId}
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
                <>
                  <input
                    id={inputId}
                    name={input.label}
                    type="number"
                    step={numericInputAdjustmentStep(input)}
                    min={
                      numericInputAdjustmentStep(input) === "any"
                        ? (input.options.lower ?? undefined)
                        : undefined
                    }
                    max={input.options.upper ?? undefined}
                    required
                    aria-valuemin={input.options.lower ?? undefined}
                    aria-valuemax={input.options.upper ?? undefined}
                    aria-describedby={hintId}
                    disabled={disabled || answered || Boolean(submittedText)}
                    value={values[input.label] ?? ""}
                    onChange={(event) => {
                      updateNumericInputRangeValidity(
                        event.currentTarget,
                        input,
                      );
                      setValues((current) => ({
                        ...current,
                        [input.label]: event.target.value,
                      }));
                    }}
                  />
                  <small className="input-adjustment-hint" id={hintId}>
                    {numericInputAdjustmentHint(input)}
                  </small>
                </>
              )}
            </div>
          );
        })}
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
          {message.response_timing ? (
            <ResponseLatency timing={message.response_timing} />
          ) : null}
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
  medicationSideEffectEnabled,
  days,
  historyLoading,
  chatSending,
  chatError,
  chatErrorRetryable,
  activeResponseTiming,
  failedResponseTiming,
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
        medicationSideEffectEnabled={medicationSideEffectEnabled}
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
                    {activeResponseTiming ? (
                      <ResponseLatency timing={activeResponseTiming} />
                    ) : null}
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
              <div className="chat-runtime-error-heading">
                <strong>AI 응답을 받지 못했습니다.</strong>
                {failedResponseTiming ? (
                  <ResponseLatency timing={failedResponseTiming} />
                ) : null}
              </div>
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
            {medicationSideEffectEnabled ? (
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
            ) : null}
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

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  DashboardData,
  MedicationScenario,
  PolicyKey,
} from "../api/contracts";
import {
  formatKoreanCalendarDate,
  formatKoreaTime,
  koreaDateKey,
} from "../utils/koreaTime";

interface HomePageProps {
  dashboard: DashboardData;
  scenarios: MedicationScenario[];
  onAdvanceClock: (minutes: 30 | 180) => Promise<void>;
  onApplyScenario: (scenarioId: string) => Promise<void>;
  onTakeDose: (doseEventId: string) => Promise<void>;
  onUpdatePolicy: (policy: PolicyKey, enabled: boolean) => Promise<void>;
  onAcknowledgeNotifications: () => Promise<void>;
  onOpenNotificationChat: (messageId: string) => Promise<void>;
  onResetTestbed: () => Promise<void>;
  disabled?: boolean;
}

function scenarioDescription(scenario: MedicationScenario): string {
  if (!scenario.medications.length) {
    return "등록된 복약 항목 없음";
  }
  if (scenario.medications.length === 1) {
    const medication = scenario.medications[0];
    return `${medication.medication_name} ${medication.dosage}`.trim();
  }
  return `${scenario.medications.length}개 복약 항목`;
}

function isWarningNotification(
  notification: DashboardData["notifications"][number],
): boolean {
  return (
    notification.metadata.severity === "warning" ||
    notification.metadata.severity === "critical" ||
    notification.notification_type.includes("warning") ||
    notification.notification_type.includes("adverse")
  );
}

function isWarningNutritionMetric(
  metric: DashboardData["nutrition"]["metrics"][number],
): boolean {
  return metric.is_warning || metric.daily_exceeded || metric.meal_exceeded;
}

export default function HomePage({
  dashboard,
  scenarios,
  onAdvanceClock,
  onApplyScenario,
  onTakeDose,
  onUpdatePolicy,
  onAcknowledgeNotifications,
  onOpenNotificationChat,
  onResetTestbed,
  disabled = false,
}: HomePageProps) {
  const summaryTotalMeals =
    typeof dashboard.nutrition.summary.total_meals === "number"
      ? dashboard.nutrition.summary.total_meals
      : dashboard.nutrition.meals.length;
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [selectedScenarioId, setSelectedScenarioId] = useState(
    dashboard.active_scenario?.scenario_id ??
      scenarios[0]?.scenario_id ??
      "",
  );
  const [busyAction, setBusyAction] = useState<string | null>(null);

  useEffect(() => {
    setSelectedScenarioId((current) => {
      const activeScenarioId = dashboard.active_scenario?.scenario_id;
      if (
        activeScenarioId &&
        scenarios.some(
          (scenario) => scenario.scenario_id === activeScenarioId,
        )
      ) {
        return activeScenarioId;
      }
      if (
        current &&
        scenarios.some((scenario) => scenario.scenario_id === current)
      ) {
        return current;
      }
      return scenarios[0]?.scenario_id ?? "";
    });
  }, [dashboard.active_scenario?.scenario_id, scenarios]);

  const scheduleDate =
    dashboard.active_scenario?.schedule_date ??
    koreaDateKey(dashboard.clock.current_time);
  const orderedMetrics = useMemo(() => {
    const preferred = ["칼로리", "단백질", "나트륨", "지방"];
    const preferredMetrics = preferred
      .map((name) =>
        dashboard.nutrition.metrics.find((metric) => metric.name === name),
      )
      .filter(
        (
          metric,
        ): metric is DashboardData["nutrition"]["metrics"][number] =>
          metric !== undefined,
      );
    const preferredNames = new Set(
      preferredMetrics.map((metric) => metric.name),
    );
    return [
      ...preferredMetrics,
      ...dashboard.nutrition.metrics.filter(
        (metric) => !preferredNames.has(metric.name),
      ),
    ].slice(0, 4);
  }, [dashboard.nutrition.metrics]);
  const meal = dashboard.nutrition.meals[0];
  const mealFoodNames =
    meal?.foods
      .map((food) => food.food_name.trim())
      .filter((foodName) => foodName.length > 0)
      .join(", ") ?? "";
  const mealDisplayName =
    mealFoodNames || meal?.description.trim() || "식사 기록";
  const mealCalories = meal?.foods.reduce(
    (total, food) =>
      total + (food.nutrients["칼로리"]?.value ?? 0),
    0,
  );
  const activeScenario = scenarios.find(
    (scenario) =>
      scenario.scenario_id === dashboard.active_scenario?.scenario_id,
  );
  const hasNutritionWarning = dashboard.nutrition.metrics.some(
    isWarningNutritionMetric,
  );
  const newestNotifications = dashboard.notifications.slice(0, 2);
  const activePolicyCount = Object.values(dashboard.policies).filter(
    Boolean,
  ).length;

  function medicationDisplayName(
    medication: DashboardData["medications"][number],
  ): string {
    const item = activeScenario?.medications.find(
      (candidate) =>
        candidate.medication_name === medication.medication_name &&
        candidate.slot_label === medication.slot_label,
    );
    if (!item?.dosage || medication.medication_name.includes(item.dosage)) {
      return medication.medication_name;
    }
    return `${medication.medication_name} ${item.dosage}`;
  }

  async function runAction(key: string, action: () => Promise<void>) {
    if (disabled || busyAction) {
      return;
    }
    setBusyAction(key);
    try {
      await action();
    } catch {
      // The parent keeps the prior state and presents the existing toast.
    } finally {
      setBusyAction(null);
    }
  }

  function openScheduleDialog() {
    setSelectedScenarioId(
      dashboard.active_scenario?.scenario_id ??
        scenarios[0]?.scenario_id ??
        "",
    );
    dialogRef.current?.showModal();
  }

  function confirmTestbedReset() {
    const confirmed = window.confirm(
      "현재 테스트 복약 일정, 복약 기록, 알림, 대화를 모두 초기화할까요?",
    );
    if (!confirmed) {
      return;
    }
    void runAction("reset", onResetTestbed);
  }

  async function submitSchedule(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const selected = scenarios.find(
      (scenario) => scenario.scenario_id === selectedScenarioId,
    );
    if (!selected) {
      return;
    }
    await runAction("schedule", async () => {
      await onApplyScenario(selectedScenarioId);
      dialogRef.current?.close();
    });
  }

  return (
    <>
      <div className="app-layout">
        <aside className="sidebar-stack">
          <section className="panel medication-panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">MEDICATION</p>
                <h2>오늘 복약 일정</h2>
              </div>
            </div>

            <div
              id="today-medication-list"
              className="medication-list"
              aria-label="오늘 복약 일정"
            >
              {dashboard.medications.map((medication) => {
                const taken = medication.status === "taken";
                const missed = medication.status === "missed";
                return (
                  <article
                    className={`medication-item${
                      taken ? " is-complete" : missed ? " is-missed" : ""
                    }`}
                    key={medication.dose_event_id}
                  >
                    <div>
                      <strong>{medicationDisplayName(medication)}</strong>
                    </div>
                    <div className="medication-label-column">
                      <span className="medication-category-badge">
                        {medication.treatment_area}
                      </span>
                      <span className="item-state">
                        {taken ? "복용" : missed ? "미복용" : "예정"}
                      </span>
                    </div>
                  </article>
                );
              })}
              {!dashboard.medications.length ? (
                <p className="runtime-empty-state">
                  오늘 등록된 복약 일정이 없습니다.
                </p>
              ) : null}
            </div>
          </section>

          <section className="panel simulation-panel">
            <div className="panel-header">
              <div>
                <p className="panel-kicker">TESTBED</p>
                <h2>시뮬레이션 시간</h2>
              </div>
              <span
                className={`badge ${
                  dashboard.clock.is_running ? "success" : "neutral"
                }`}
              >
                {dashboard.clock.is_running ? "실행 중" : "일시 정지"}
              </span>
            </div>
            <div className="simulation-clock">
              <span>현재 시각</span>
              <strong>{formatKoreaTime(dashboard.clock.current_time)}</strong>
              <small>실행 중에는 1초마다 60분씩 진행합니다.</small>
            </div>
            <div className="button-grid">
              <button
                type="button"
                disabled={disabled || busyAction !== null}
                onClick={() =>
                  void runAction("advance-30", () => onAdvanceClock(30))
                }
              >
                30분 진행
              </button>
              <button
                type="button"
                disabled={disabled || busyAction !== null}
                onClick={() =>
                  void runAction("advance-180", () => onAdvanceClock(180))
                }
              >
                3시간 진행
              </button>
            </div>
            <button
              className="wide-button secondary schedule-setup-button"
              type="button"
              disabled={
                disabled || busyAction !== null || !scenarios.length
              }
              onClick={openScheduleDialog}
            >
              테스트 복약 일정 설정
            </button>
            <p className="current-schedule">
              현재 적용:{" "}
              <strong>
                {dashboard.active_scenario?.name ?? "설정 전"}
              </strong>
            </p>
            <button
              className="wide-button reset-outline"
              type="button"
              disabled={disabled || busyAction !== null}
              onClick={confirmTestbedReset}
            >
              {busyAction === "reset" ? "초기화 중..." : "전체 리셋"}
            </button>
          </section>
        </aside>

        <section className="content-shell">
          <div className="main-column">
            <section className="panel timeline-panel">
              <div className="panel-header">
                <div>
                  <p className="panel-kicker">TODAY</p>
                  <h2>복약 타임라인</h2>
                  <p className="panel-subtitle">
                    {formatKoreanCalendarDate(scheduleDate)}
                  </p>
                </div>
              </div>

              <div
                className="timeline-scroll"
                tabIndex={0}
                aria-label="오늘 복약 일정, 4개 이후 스크롤"
              >
                <ol id="medication-timeline" className="timeline">
                  {dashboard.medications.map((medication) => {
                    const taken = medication.status === "taken";
                    const missed = medication.status === "missed";
                    return (
                      <li
                        className={`timeline-item ${
                          taken
                            ? "status-taken"
                            : missed
                              ? "status-missed"
                              : "status-upcoming"
                        }`}
                        key={medication.dose_event_id}
                      >
                        <div
                          className="timeline-marker"
                          aria-hidden="true"
                        />
                        <time dateTime={medication.scheduled_for}>
                          {formatKoreaTime(medication.scheduled_for)}
                        </time>
                        <div className="timeline-content">
                          <strong>
                            {medication.slot_label} 복약
                            {taken ? " 완료" : missed ? " 미복용" : ""}
                          </strong>
                          <p>
                            {medicationDisplayName(medication)} ·{" "}
                            {medication.treatment_area}
                          </p>
                        </div>
                        {taken ? (
                          <span className="tiny-badge success">복용</span>
                        ) : (
                          <div className="timeline-actions">
                            {missed ? (
                              <span className="tiny-badge missed">미복용</span>
                            ) : null}
                            <button
                              className="compact-button"
                              type="button"
                              disabled={disabled || busyAction !== null}
                              onClick={() =>
                                void runAction(
                                  `dose-${medication.dose_event_id}`,
                                  () =>
                                    onTakeDose(medication.dose_event_id),
                                )
                              }
                            >
                              복용 기록
                            </button>
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ol>
                {!dashboard.medications.length ? (
                  <p className="runtime-empty-state">
                    표시할 복약 타임라인이 없습니다.
                  </p>
                ) : null}
              </div>
            </section>

            <section className="panel nutrition-panel">
              <div className="panel-header">
                <div>
                  <p className="panel-kicker">NUTRITION</p>
                  <h2>오늘 영양 상태</h2>
                  <p className="panel-subtitle">
                    식사 {summaryTotalMeals}건 기록
                  </p>
                </div>
                <span
                  className={`badge ${
                    hasNutritionWarning ? "warning" : "success"
                  }`}
                >
                  {hasNutritionWarning ? "주의" : "정상"}
                </span>
              </div>
              <div className="nutrition-grid">
                {orderedMetrics.map((metric) => {
                  const warning = isWarningNutritionMetric(metric);
                  return (
                    <article
                      className={`nutrition-card${
                        warning ? " is-warning" : ""
                      }`}
                      key={metric.name}
                    >
                      <div>
                        <strong>{metric.name}</strong>
                        <span>{Math.round(metric.percent)}%</span>
                      </div>
                      <p>
                        <b>{metric.intake.toLocaleString("ko-KR")}</b> /{" "}
                        {metric.threshold.toLocaleString("ko-KR")} {metric.unit}
                      </p>
                      <div className="metric-bar">
                        <span
                          style={{
                            width: `${Math.min(100, metric.bar_percent)}%`,
                          }}
                        />
                      </div>
                    </article>
                  );
                })}
              </div>
              {!orderedMetrics.length ? (
                <p className="runtime-empty-state">
                  오늘 집계된 영양 정보가 없습니다.
                </p>
              ) : null}
              {meal ? (
                <div className="meal-summary">
                  <span className="meal-icon" aria-hidden="true">
                    {meal.meal_label}
                  </span>
                  <div>
                    <strong>{mealDisplayName}</strong>
                    <p>
                      {meal.meal_time} 기록
                      {mealCalories
                        ? ` · ${mealCalories.toLocaleString("ko-KR")} kcal`
                        : ""}
                    </p>
                  </div>
                </div>
              ) : null}
            </section>
          </div>

          <aside className="monitor-column">
            <section className="panel policy-panel">
              <div className="panel-header">
                <div>
                  <p className="panel-kicker">POLICY</p>
                  <h2>활성 알림 정책</h2>
                </div>
                <span className="badge neutral">
                  {activePolicyCount}개 활성
                </span>
              </div>
              <article className="policy-card">
                <div className="policy-card-head">
                  <strong>복약 예정 알림</strong>
                  <label className="switch">
                    <input
                      type="checkbox"
                      checked={dashboard.policies.medication_schedule_alert}
                      disabled={disabled || busyAction !== null}
                      aria-label="복약 예정 알림 켜기"
                      onChange={(event) =>
                        void runAction("policy-schedule", () =>
                          onUpdatePolicy(
                            "medication_schedule_alert",
                            event.target.checked,
                          ),
                        )
                      }
                    />
                    <span />
                  </label>
                </div>
                <p>복약 시간 30분 전에 알려드려요.</p>
                <div className="policy-meta">
                  <span>매일</span>
                  <span>30분 전</span>
                  <span>앱 알림</span>
                </div>
              </article>
              <article className="policy-card">
                <div className="policy-card-head">
                  <strong>미복용 확인 대화</strong>
                  <label className="switch">
                    <input
                      type="checkbox"
                      checked={dashboard.policies.missed_dose_conversation}
                      disabled={disabled || busyAction !== null}
                      aria-label="미복용 확인 대화 켜기"
                      onChange={(event) =>
                        void runAction("policy-missed", () =>
                          onUpdatePolicy(
                            "missed_dose_conversation",
                            event.target.checked,
                          ),
                        )
                      }
                    />
                    <span />
                  </label>
                </div>
                <p>복약 기록이 없으면 AI가 이유를 확인해요.</p>
                <div className="policy-meta">
                  <span>90분 후</span>
                  <span>AI 대화</span>
                </div>
              </article>
            </section>

            <section className="panel notification-panel">
              <div className="panel-header">
                <div>
                  <p className="panel-kicker notification-kicker">
                    NOTIFICATION
                  </p>
                  <h2>알림 센터</h2>
                </div>
                <span className="badge notify">
                  {dashboard.notifications.length}건
                </span>
              </div>
              <div className="notification-list">
                {newestNotifications.map((notification, index) => {
                  const warning = isWarningNotification(notification);
                  return (
                    <article
                      className={`notification${
                        notification.acknowledged
                          ? ""
                          : " is-unacknowledged"
                      }${warning ? " is-warning" : ""}`}
                      key={notification.id}
                    >
                      <div
                        className={`notification-icon${
                          index === 0 ? "" : " is-soft"
                        }`}
                        aria-hidden="true"
                      >
                        {index === 0 ? "!" : "✓"}
                      </div>
                      <div className="notification-content">
                        <div className="notification-title">
                          <strong>{notification.title}</strong>
                          <time dateTime={notification.visible_at}>
                            {notification.visible_at_label.slice(-5)}
                          </time>
                        </div>
                        <p>{notification.body}</p>
                        {notification.interaction?.kind === "open_chat" ? (
                          <button
                            className="notification-chat-button"
                            type="button"
                            disabled={
                              disabled ||
                              busyAction !== null ||
                              notification.interaction.state !== "ready" ||
                              !notification.interaction.message_id
                            }
                            onClick={() => {
                              const messageId =
                                notification.interaction?.message_id;
                              if (!messageId) {
                                return;
                              }
                              void runAction(
                                `notification-chat-${notification.id}`,
                                () => onOpenNotificationChat(messageId),
                              );
                            }}
                          >
                            {notification.interaction.state === "ready"
                              ? "대화 확인"
                              : notification.interaction.state === "pending"
                                ? "대화 준비 중"
                                : "대화 생성 실패"}
                          </button>
                        ) : null}
                      </div>
                    </article>
                  );
                })}
                {!newestNotifications.length ? (
                  <p className="runtime-empty-state">
                    확인할 새 알림이 없습니다.
                  </p>
                ) : null}
              </div>
              <button
                className="wide-button notify-outline"
                type="button"
                disabled={disabled || busyAction !== null}
                onClick={() =>
                  void runAction("notifications", onAcknowledgeNotifications)
                }
              >
                모두 확인
              </button>
            </section>
          </aside>
        </section>
      </div>

      <dialog
        ref={dialogRef}
        className="schedule-dialog"
        aria-labelledby="schedule-dialog-title"
      >
        <div className="schedule-dialog-header">
          <div>
            <p className="panel-kicker">TEST SCHEDULE</p>
            <h2 id="schedule-dialog-title">테스트 복약 일정 설정</h2>
          </div>
          <button
            className="dialog-close-button"
            type="button"
            aria-label="닫기"
            onClick={() => dialogRef.current?.close()}
          >
            ×
          </button>
        </div>

        <p className="schedule-dialog-description">
          DB에 준비된 테스트 예시를 현재 시뮬레이션 날짜에 적용합니다.
        </p>

        <form onSubmit={(event) => void submitSchedule(event)}>
          <fieldset className="scenario-fieldset">
            <legend>시나리오 선택</legend>
            <div className="scenario-options">
              {scenarios.map((scenario) => (
                <label className="scenario-option" key={scenario.scenario_id}>
                  <input
                    type="radio"
                    name="schedule_scenario"
                    value={scenario.scenario_id}
                    checked={selectedScenarioId === scenario.scenario_id}
                    onChange={() =>
                      setSelectedScenarioId(scenario.scenario_id)
                    }
                  />
                  <span className="scenario-option-copy">
                    <strong>{scenario.name}</strong>
                    <small>{scenarioDescription(scenario)}</small>
                  </span>
                  <span className="scenario-option-check" aria-hidden="true">
                    ✓
                  </span>
                </label>
              ))}
              {!scenarios.length ? (
                <p className="runtime-empty-state">
                  Backend에 등록된 테스트 복약 일정이 없습니다.
                </p>
              ) : null}
            </div>
          </fieldset>

          <div className="schedule-date-row">
            <span>적용 기준일</span>
            <time dateTime={scheduleDate}>
              {formatKoreanCalendarDate(scheduleDate)}
            </time>
          </div>

          <div className="schedule-dialog-actions">
            <button
              className="compact-button ghost"
              type="button"
              onClick={() => dialogRef.current?.close()}
            >
              취소
            </button>
            <button
              className="compact-button"
              type="submit"
              disabled={
                disabled ||
                busyAction !== null ||
                !selectedScenarioId
              }
            >
              일정 적용
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}

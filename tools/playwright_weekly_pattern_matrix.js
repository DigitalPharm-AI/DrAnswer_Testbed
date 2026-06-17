const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `weekly-pattern-matrix-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

const scenarios = [
  {
    id: "morning_missed_apply_increase",
    title: "아침 08:00 7일 연속 미복용 후 알림 강화 적용",
    timesCsv: "08:00",
    expectedSlots: ["아침 08:00"],
    expectedMissedCount: 7,
    expectedPolicyConfirmationCount: 6,
    finalAction: "increase",
    expectedApplied: true,
    expectation: "7일 누적 daily pattern 확인에서 늘리기를 선택하면 아침 맞춤 정책이 적용된다.",
  },
  {
    id: "morning_missed_keep_current",
    title: "아침 08:00 7일 연속 미복용 후 현행 유지",
    timesCsv: "08:00",
    expectedSlots: ["아침 08:00"],
    expectedMissedCount: 7,
    expectedPolicyConfirmationCount: 6,
    finalAction: "keep",
    expectedApplied: false,
    expectation: "7일 누적 daily pattern 확인에서 유지하기를 선택하면 맞춤 정책이 생기지 않는다.",
  },
  {
    id: "night_missed_apply_increase",
    title: "야간 21:00 7일 연속 미복용 후 알림 강화 적용",
    timesCsv: "21:00",
    expectedSlots: ["야간 21:00"],
    expectedMissedCount: 7,
    expectedPolicyConfirmationCount: 6,
    finalAction: "increase",
    expectedApplied: true,
    expectation: "야간 슬롯만 미복용되면 야간 슬롯 정책 후보만 생성되고 적용된다.",
  },
  {
    id: "morning_and_night_missed_apply_increase",
    title: "아침 08:00 + 야간 21:00 7일 연속 미복용 후 복수 정책 적용",
    timesCsv: "08:00,21:00",
    expectedSlots: ["아침 08:00", "야간 21:00"],
    expectedMissedCount: 14,
    expectedPolicyConfirmationCount: 6,
    finalAction: "increase",
    expectedApplied: true,
    expectation: "두 슬롯 모두 7일간 미복용되면 두 슬롯 정책 후보가 함께 생성되고 함께 적용된다.",
  },
];

const results = [];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getJson(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function getText(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.text();
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

async function acknowledgeNotification(notificationId) {
  await postForm(`/notifications/${notificationId}/ack`);
}

async function waitFor(name, predicate, timeoutMs = 60000, intervalMs = 500) {
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

function plainText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
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

function maxNotificationId(payload) {
  const rows = payload.notifications || [];
  return rows.reduce((max, row) => Math.max(max, Number(row.id || 0)), 0);
}

async function feed(afterId = 0) {
  return getJson(`/api/notifications/feed?after_id=${afterId}`);
}

async function waitForNewConversationAlerts(afterId, timeoutMs = 60000) {
  return waitFor(
    `conversation_alerts_after_${afterId}`,
    async () => {
      const payload = await feed(afterId);
      const alerts = (payload.notifications || []).filter((row) => row.notification_type === "conversation_alert");
      return alerts.length ? payload : false;
    },
    timeoutMs,
    500,
  );
}

async function advanceUntilNewConversationAlerts(afterId) {
  const advances = [1440, 60, 60, 60, 180, 720];
  for (const minutes of advances) {
    await postForm("/clock/advance", { minutes: String(minutes) });
    try {
      return await waitForNewConversationAlerts(afterId, 8000);
    } catch (_) {
      // Continue through daily-pattern checkpoints that do not create a visible conversation.
    }
  }
  return waitForNewConversationAlerts(afterId, 15000);
}

async function choosePolicyAction(action) {
  const html = await waitFor(
    `policy_form_${action}`,
    async () => {
      const currentHtml = await getText("/partials/chat-log");
      const forms = extractForms(currentHtml, "/chat/policy-confirmation");
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
      return selected ? currentHtml : false;
    },
    15000,
    500,
  );
  const forms = extractForms(html, "/chat/policy-confirmation");
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
}

function candidateSlotsFrom(metadata) {
  const raw = metadata && Array.isArray(metadata.proposed_policies) ? metadata.proposed_policies : [];
  return raw.map((policy) => String(policy.slot_label || "")).filter(Boolean);
}

function assertPolicyCandidateSlots(scenario, metadata) {
  const candidateSlots = candidateSlotsFrom(metadata);
  const missing = scenario.expectedSlots.filter((slot) => !candidateSlots.includes(slot));
  if (missing.length > 0) {
    throw new Error(`policy_candidate_missing:${scenario.id}:${missing.join(",")}:${JSON.stringify(metadata)}`);
  }
  const unexpected = candidateSlots.filter((slot) => !scenario.expectedSlots.includes(slot));
  if (unexpected.length > 0) {
    throw new Error(`policy_candidate_unexpected:${scenario.id}:${unexpected.join(",")}:${JSON.stringify(metadata)}`);
  }
  return candidateSlots;
}

async function setupMedicationForScenario(page, scenario) {
  await postForm("/simulation/reset");
  const current = await feed(0);
  const startDate = current.current_time.slice(0, 10);
  await postForm("/medications", {
    medication_choice: "당뇨약",
    dosage_choice: "1정",
    start_date: startDate,
    end_date: "2026-04-26",
    schedule_template: "custom",
    times_csv: scenario.timesCsv,
    instructions: `weekly pattern matrix: ${scenario.id}`,
  });
  await postForm("/phr/register");
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
  await page.waitForFunction(
    () => Array.from(document.querySelectorAll(".med-title")).some((node) => (node.textContent || "").includes("당뇨약")),
    { timeout: 10000 },
  );
  await page.screenshot({ path: path.join(OUT_DIR, `${scenario.id}-01-setup.png`), fullPage: true });
  return { startDate };
}

async function advanceSevenDailyPatterns(scenario) {
  let lastSeen = maxNotificationId(await feed(0));
  const missedDoseEventIds = new Set();
  const policyConfirmations = [];
  const observedCandidateSlots = [];

  for (let step = 0; step < 50 && policyConfirmations.length < scenario.expectedPolicyConfirmationCount; step += 1) {
    const initialPayload = await advanceUntilNewConversationAlerts(lastSeen);
    await sleep(750);
    const payload = await feed(lastSeen);
    const notifications = payload.notifications && payload.notifications.length ? payload.notifications : initialPayload.notifications || [];
    lastSeen = Math.max(lastSeen, payload.last_seen_id || initialPayload.last_seen_id || maxNotificationId(payload));

    for (const notification of notifications) {
      const metadata = notification.metadata || {};
      if (notification.notification_type !== "conversation_alert") {
        continue;
      }
      if (metadata.category === "missed_dose" && notification.related_dose_event_id) {
        missedDoseEventIds.add(Number(notification.related_dose_event_id));
        await acknowledgeNotification(notification.id);
        continue;
      }
      if (metadata.category !== "policy_confirmation") {
        continue;
      }
      const candidateSlots = assertPolicyCandidateSlots(scenario, metadata);
      observedCandidateSlots.push(candidateSlots);
      policyConfirmations.push(notification.id);
      if (policyConfirmations.length < scenario.expectedPolicyConfirmationCount) {
        await choosePolicyAction("keep");
      } else {
        break;
      }
    }
  }

  if (missedDoseEventIds.size !== scenario.expectedMissedCount) {
    throw new Error(`missed_dose_count_mismatch:${scenario.id}:expected=${scenario.expectedMissedCount}:actual=${missedDoseEventIds.size}`);
  }
  if (policyConfirmations.length !== scenario.expectedPolicyConfirmationCount) {
    throw new Error(
      `policy_confirmation_count_mismatch:${scenario.id}:expected=${scenario.expectedPolicyConfirmationCount}:actual=${policyConfirmations.length}`,
    );
  }

  return {
    missedDoseCount: missedDoseEventIds.size,
    policyConfirmationCount: policyConfirmations.length,
    finalPolicyNotificationId: policyConfirmations[policyConfirmations.length - 1],
    observedCandidateSlots,
  };
}

async function assertPolicyOutcome(page, scenario) {
  const beforeActivePolicies = plainText(await getText("/partials/active-policies"));
  if (beforeActivePolicies.includes("맞춤 정책")) {
    throw new Error(`pattern_policy_applied_before_confirmation:${scenario.id}:${beforeActivePolicies}`);
  }

  await choosePolicyAction(scenario.finalAction);

  if (scenario.expectedApplied) {
    await waitFor(
      `pattern_policy_applied_${scenario.id}`,
      async () => {
        const text = plainText(await getText("/partials/active-policies"));
        return scenario.expectedSlots.every((slot) => text.includes(slot)) && text.includes("맞춤 정책") && text.includes("2회 추가");
      },
      15000,
      500,
    );
  } else {
    await sleep(1000);
    const text = plainText(await getText("/partials/active-policies"));
    if (text.includes("맞춤 정책")) {
      throw new Error(`pattern_policy_applied_despite_keep:${scenario.id}:${text}`);
    }
  }

  await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
  await page.screenshot({ path: path.join(OUT_DIR, `${scenario.id}-03-outcome.png`), fullPage: true });

  return {
    finalAction: scenario.finalAction,
    expectedApplied: scenario.expectedApplied,
    activePolicyText: plainText(await getText("/partials/active-policies")),
  };
}

async function runScenario(page, scenario) {
  const started = Date.now();
  console.log(`SCENARIO ${scenario.id} start`);
  try {
    const setup = await setupMedicationForScenario(page, scenario);
    const pattern = await advanceSevenDailyPatterns(scenario);
    await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.screenshot({ path: path.join(OUT_DIR, `${scenario.id}-02-policy-confirmations.png`), fullPage: true });
    const outcome = await assertPolicyOutcome(page, scenario);
    const elapsedMs = Date.now() - started;
    const result = {
      id: scenario.id,
      title: scenario.title,
      status: "passed",
      elapsedMs,
      expectation: scenario.expectation,
      setup,
      pattern,
      outcome,
      screenshots: [
        `${scenario.id}-01-setup.png`,
        `${scenario.id}-02-policy-confirmations.png`,
        `${scenario.id}-03-outcome.png`,
      ],
    };
    results.push(result);
    console.log(`PASS ${scenario.id} ${elapsedMs}ms`);
  } catch (error) {
    const elapsedMs = Date.now() - started;
    const screenshot = `${scenario.id}-failed.png`;
    try {
      await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
    } catch (_) {
      // Best effort artifact.
    }
    results.push({
      id: scenario.id,
      title: scenario.title,
      status: "failed",
      elapsedMs,
      expectation: scenario.expectation,
      error: error.message || String(error),
      screenshots: [screenshot],
    });
    console.error(`FAIL ${scenario.id} ${elapsedMs}ms ${error.message || error}`);
  }
}

function markdownEscape(value) {
  return String(value || "").replace(/\|/g, "\\|").replace(/\n/g, " ");
}

function writeReport(summary) {
  const now = new Date().toISOString();
  const lines = [];
  lines.push("# 7일 복약 패턴 정책 변경 Playwright 리포트");
  lines.push("");
  lines.push(`- 실행 시각: ${now}`);
  lines.push(`- 대상 URL: ${BASE_URL}`);
  lines.push(`- 출력 폴더: ${OUT_DIR}`);
  lines.push("- 테스트 방식: 실제 브라우저에서 시스템 앱을 열고, 시뮬레이션 시간을 7일치 진행하며 notification feed와 채팅 정책 확인 폼을 검증");
  lines.push("- Agent provider: 테스트 실행 환경의 `LLM_PROVIDER` 값 사용. 이번 시나리오는 재현성을 위해 `rule_based` provider에서 돌리는 것을 전제로 작성");
  lines.push("");
  lines.push("## 해석 기준");
  lines.push("");
  lines.push("현재 구현은 매일 한 번 daily pattern job을 만들되, payload에는 해당 시점에 관측 가능한 최근 최대 7일 rolling window를 담는다. 관측일이 2일 이상일 때부터 정책 후보가 생성되므로, 7일 미복용 시 정책 확인은 6회 생성된다. 이 테스트는 7일치 미복용과 충분한 관측 이후의 정책 확인 흐름을 검증한다.");
  lines.push("");
  lines.push("## 결과 요약");
  lines.push("");
  lines.push("| 시나리오 | 상태 | 미복용 이벤트 | 정책 확인 | 최종 선택 | 정책 적용 |");
  lines.push("|---|---:|---:|---:|---|---|");
  for (const result of summary.results) {
    const pattern = result.pattern || {};
    const outcome = result.outcome || {};
    lines.push(
      `| ${markdownEscape(result.title)} | ${result.status} | ${pattern.missedDoseCount ?? "-"} | ${pattern.policyConfirmationCount ?? "-"} | ${outcome.finalAction || "-"} | ${outcome.expectedApplied === true ? "적용" : outcome.expectedApplied === false ? "미적용" : "-"} |`,
    );
  }
  lines.push("");
  lines.push(`- 통과: ${summary.passed}`);
  lines.push(`- 실패: ${summary.failed}`);
  lines.push("");
  lines.push("## 시나리오별 관찰");
  lines.push("");
  for (const result of summary.results) {
    lines.push(`### ${result.title}`);
    lines.push("");
    lines.push(`- 상태: ${result.status}`);
    lines.push(`- 기대: ${result.expectation}`);
    if (result.status === "passed") {
      lines.push(`- 관찰된 미복용 이벤트 수: ${result.pattern.missedDoseCount}`);
      lines.push(`- 관찰된 정책 확인 수: ${result.pattern.policyConfirmationCount}`);
      lines.push(`- 최종 정책 확인 notification id: ${result.pattern.finalPolicyNotificationId}`);
      lines.push(`- 후보 슬롯: ${result.pattern.observedCandidateSlots.map((slots) => slots.join(", ")).join(" / ")}`);
      lines.push(`- 최종 active policy 요약: ${result.outcome.activePolicyText}`);
    } else {
      lines.push(`- 오류: ${result.error}`);
    }
    lines.push(`- 스크린샷: ${result.screenshots.map((name) => path.join(OUT_DIR, name)).join(", ")}`);
    lines.push("");
  }
  lines.push("## 결론");
  lines.push("");
  if (summary.failed === 0) {
    lines.push("검증한 모든 시나리오에서 7일치 미복용과 6회의 daily pattern 정책 확인이 생성됐다. 정책 변경은 사용자 확인 전에는 적용되지 않았고, 사용자가 `늘리기`를 선택한 경우에만 해당 슬롯의 맞춤 정책이 생성됐다. `유지하기` 선택 시에는 active policy가 바뀌지 않았다.");
  } else {
    lines.push("일부 시나리오가 실패했다. 실패 항목의 스크린샷과 오류 메시지를 기준으로 daily pattern 생성, 정책 후보 생성, 또는 confirmation 적용 경로를 추가 점검해야 한다.");
  }
  lines.push("");
  lines.push("## 남은 확인 포인트");
  lines.push("");
  lines.push("- 이 테스트는 deterministic provider 기반이다. Bedrock real agent로 같은 테스트를 돌리면 LLM 판단 변동성 때문에 후보 정책 문구나 개수가 달라질 수 있다.");
  lines.push("- 현재 구조는 daily job 이름을 유지하면서 rolling 7-day window payload를 보내는 방식이다. 별도의 weekly job 이름이나 별도 저장 테이블이 필요하다면 모델/스케줄러 이름을 추가로 분리할 수 있다.");

  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
}

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

  try {
    for (const scenario of scenarios) {
      await runScenario(page, scenario);
    }
  } finally {
    await browser.close();
  }

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

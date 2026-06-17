const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `weekly-pattern-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

const results = [];

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
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
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

async function choosePolicyAction(action) {
  const html = await getText("/partials/chat-log");
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

async function runScenario(page, name, fn) {
  const started = Date.now();
  try {
    const details = await fn();
    const elapsedMs = Date.now() - started;
    results.push({ name, status: "passed", elapsedMs, details });
    console.log(`PASS ${name} ${elapsedMs}ms`);
  } catch (error) {
    const elapsedMs = Date.now() - started;
    const screenshot = path.join(OUT_DIR, `${results.length + 1}-${name.replace(/[^a-z0-9_-]+/gi, "_")}.png`);
    try {
      await page.screenshot({ path: screenshot, fullPage: true });
    } catch (_) {
      // Best effort artifact.
    }
    results.push({ name, status: "failed", elapsedMs, error: error.message || String(error), screenshot });
    console.error(`FAIL ${name} ${elapsedMs}ms ${error.message || error}`);
  }
}

async function waitForNewNotifications(afterId) {
  return waitFor(
    `notifications_after_${afterId}`,
    async () => {
      const payload = await feed(afterId);
      return (payload.notifications || []).length ? payload : false;
    },
    45000,
    500,
  );
}

async function advanceThroughSevenDayPattern() {
  let lastSeen = maxNotificationId(await feed(0));
  const missedDoseEventIds = new Set();
  const handledPolicyIds = [];
  let finalPolicyId = null;

  for (let step = 0; step < 30 && handledPolicyIds.length < 7; step += 1) {
    await postForm("/clock/advance", { minutes: "1440" });
    const payload = await waitForNewNotifications(lastSeen);
    const notifications = payload.notifications || [];
    lastSeen = Math.max(lastSeen, payload.last_seen_id || maxNotificationId(payload));

    for (const notification of notifications) {
      const metadata = notification.metadata || {};
      if (notification.notification_type !== "conversation_alert") {
        continue;
      }
      if (metadata.category === "missed_dose" && notification.related_dose_event_id) {
        missedDoseEventIds.add(Number(notification.related_dose_event_id));
        continue;
      }
      if (metadata.category !== "policy_confirmation") {
        continue;
      }
      const proposed = metadata.proposed_policies || [];
      if (!proposed.some((policy) => String(policy.slot_label || "").includes("아침 08:00"))) {
        throw new Error(`morning_policy_candidate_missing:${JSON.stringify(metadata)}`);
      }
      handledPolicyIds.push(notification.id);
      if (handledPolicyIds.length < 7) {
        await choosePolicyAction("keep");
      } else {
        finalPolicyId = notification.id;
        break;
      }
    }
  }

  if (missedDoseEventIds.size < 7) {
    throw new Error(`seven_missed_doses_not_recorded:${missedDoseEventIds.size}`);
  }
  if (handledPolicyIds.length < 7 || !finalPolicyId) {
    throw new Error(`seven_policy_confirmations_not_reached:${handledPolicyIds.length}`);
  }
  return {
    missedDoseCount: missedDoseEventIds.size,
    policyConfirmationCount: handledPolicyIds.length,
    finalPolicyId,
  };
}

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

  await runScenario(page, "01_setup_single_morning_medication_for_week", async () => {
    await postForm("/simulation/reset");
    const current = await feed(0);
    const startDate = current.current_time.slice(0, 10);
    await postForm("/medications", {
      medication_choice: "당뇨약",
      dosage_choice: "1정",
      start_date: startDate,
      end_date: "2026-04-26",
      schedule_template: "custom",
      times_csv: "08:00",
      instructions: "weekly pattern playwright scenario",
    });
    await postForm("/phr/register");
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForFunction(() => Array.from(document.querySelectorAll(".med-title")).some((node) => (node.textContent || "").includes("당뇨약")), {
      timeout: 10000,
    });
    await page.screenshot({ path: path.join(OUT_DIR, "01-setup.png"), fullPage: true });
    return { startDate };
  });

  await runScenario(page, "02_seven_day_missed_pattern_generates_policy_confirmations", async () => {
    const result = await advanceThroughSevenDayPattern();
    await page.goto(`${BASE_URL}/#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.screenshot({ path: path.join(OUT_DIR, "02-weekly-confirmations.png"), fullPage: true });
    return result;
  });

  await runScenario(page, "03_final_confirmation_applies_pattern_policy_only_after_user_choice", async () => {
    const beforeActivePolicies = plainText(await getText("/partials/active-policies"));
    if (beforeActivePolicies.includes("맞춤 정책")) {
      throw new Error(`pattern_policy_applied_before_confirmation:${beforeActivePolicies}`);
    }
    await choosePolicyAction("increase");
    await waitFor(
      "pattern_policy_applied_after_increase",
      async () => {
        const text = plainText(await getText("/partials/active-policies"));
        return text.includes("아침 08:00") && text.includes("맞춤 정책") && text.includes("2회 추가");
      },
      15000,
      500,
    );
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.screenshot({ path: path.join(OUT_DIR, "03-policy-applied.png"), fullPage: true });
  });

  await browser.close();

  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    outputDir: OUT_DIR,
    results,
    passed: results.filter((result) => result.status === "passed").length,
    failed: results.filter((result) => result.status === "failed").length,
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  console.log(JSON.stringify(summary, null, 2));
  if (summary.failed > 0) {
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

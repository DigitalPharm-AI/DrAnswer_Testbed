const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:9000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const RUN_MUTATING_ACTIONS = process.env.RUN_MUTATING_ACTIONS === "1";
const REQUIRE_TRACE_ACTIONS = process.env.REQUIRE_TRACE_ACTIONS === "1";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `logs-readiness-actions-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

const results = [];
const consoleErrors = [];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function screenshot(page, name) {
  const fileName = `${name}.png`;
  await page.screenshot({ path: path.join(OUT_DIR, fileName), fullPage: true });
  return fileName;
}

async function runStep(page, id, title, fn) {
  const startedAt = Date.now();
  const shots = [];
  try {
    const details = await fn(shots);
    results.push({ id, title, status: "passed", elapsedMs: Date.now() - startedAt, details, screenshots: shots });
    console.log(`PASS ${id} ${title}`);
  } catch (error) {
    const failureShot = await screenshot(page, `${id}-failed`).catch(() => "");
    if (failureShot) {
      shots.push(failureShot);
    }
    results.push({
      id,
      title,
      status: "failed",
      elapsedMs: Date.now() - startedAt,
      error: error.message || String(error),
      screenshots: shots,
    });
    console.error(`FAIL ${id} ${title}: ${error.message || error}`);
  }
}

async function waitForPanelText(page, text, timeout = 30000) {
  await page.waitForFunction(
    (expected) => document.querySelector("#logs-panel")?.innerText.includes(expected),
    text,
    { timeout },
  );
}

async function clickButtonAndWait(page, buttonName, expectedText, options = {}) {
  const button = page.getByRole("button", { name: buttonName }).first();
  await button.scrollIntoViewIfNeeded();
  await button.click();
  await waitForPanelText(page, expectedText, options.timeout || 30000);
}

async function openLogs(page) {
  await page.goto(`${BASE_URL}/#logs`, { waitUntil: "domcontentloaded", timeout: 20000 });
  const logsPanel = page.locator("#logs-panel");
  if (!(await logsPanel.isVisible().catch(() => false))) {
    const logsTab = page.getByRole("button", { name: /^LOGS$/ });
    if (await logsTab.count()) {
      await logsTab.click();
    }
  }
  await page.waitForSelector("#logs-panel", { timeout: 15000 });
}

async function main() {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => consoleErrors.push(error.message || String(error)));

  await runStep(page, "01", "LOGS cards render in operator order", async (shots) => {
    await openLogs(page);
    for (const title of [
      "비동기 연동 상태",
      "Containment Action",
      "Agent Alerts",
      "Incident Timeline",
      "Trace Dashboard",
      "Prompt/Model Operations",
      "Business Metric Gate",
      "Data Quality Drift",
      "Tool/Data Catalog",
    ]) {
      await waitForPanelText(page, title);
    }
    shots.push(await screenshot(page, "01-logs-desktop"));
    const order = await page.evaluate(() =>
      Array.from(document.querySelectorAll(".logs-card h2")).map((node) => node.textContent.trim()),
    );
    return { order: order.slice(0, 12) };
  });

  await runStep(page, "02", "safe observability actions update result banner", async (shots) => {
    await clickButtonAndWait(page, "Retention dry-run 기록", "trace retention cleanup dry-run recorded");
    await clickButtonAndWait(page, "Testbed fallback 기록", "testbed model fallback drill recorded");
    await clickButtonAndWait(page, "릴리즈 리포트 생성", "release readiness report written");
    await clickButtonAndWait(page, "온라인 eval 스캔 기록", "online eval scan recorded");
    shots.push(await screenshot(page, "02-safe-actions"));
    return { actions: ["retention_dry_run", "fallback_drill", "release_report", "online_eval_scan"] };
  });

  await runStep(page, "03", "Prompt/Model Change Approval records evidence", async (shots) => {
    const card = page.locator(".logs-card-prompt-approval");
    await card.scrollIntoViewIfNeeded();
    await card.locator('select[name="change_type"]').selectOption("prompt");
    await card.locator('input[name="target"]').fill(`playwright-readiness-${RUN_ID}`);
    await card.locator('input[name="summary"]').fill("Playwright LOGS readiness action evidence");
    await card.locator('input[name="owner"]').fill("ai-ops-owner");
    await card.locator('input[name="rollback"]').fill("restore previous prompt/model config");
    await card.locator('input[name="evidence"]').fill(`playwright run ${RUN_ID}`);
    await card.getByRole("button", { name: "승인 요청 검증/기록" }).click();
    await waitForPanelText(page, "Playwright LOGS readiness action evidence");
    shots.push(await screenshot(page, "03-change-approval"));
    return { target: `playwright-readiness-${RUN_ID}` };
  });

  await runStep(page, "04", "trace detail action is stable when traces exist", async (shots) => {
    const detailButtons = page.getByRole("button", { name: "Replay 상세" });
    const count = await detailButtons.count();
    if (!count) {
      assert(!REQUIRE_TRACE_ACTIONS, "Replay 상세 button not found");
      return { skipped: true, reason: "no traces available" };
    }
    await detailButtons.first().scrollIntoViewIfNeeded();
    await detailButtons.first().click();
    await page.waitForSelector("#trace-replay-panel", { timeout: 10000 });
    const text = await page.locator("#trace-replay-panel").innerText();
    assert(text.includes("Replay plan") || text.includes("Trace") || text.length > 20, `unexpected trace detail text: ${text}`);
    shots.push(await screenshot(page, "04-trace-detail"));
    return { traceButtons: count };
  });

  await runStep(page, "05", "mobile layout keeps cards readable", async (shots) => {
    await page.setViewportSize({ width: 390, height: 900 });
    await openLogs(page);
    await waitForPanelText(page, "Incident Timeline");
    const overflowCount = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#logs-panel *")).filter((node) => node.scrollWidth > node.clientWidth + 2).length,
    );
    shots.push(await screenshot(page, "05-logs-mobile"));
    assert(overflowCount === 0, `mobile horizontal overflow nodes=${overflowCount}`);
    return { overflowCount };
  });

  if (RUN_MUTATING_ACTIONS) {
    await runStep(page, "06", "mutating operator actions remain wired", async (shots) => {
      await page.setViewportSize({ width: 1280, height: 900 });
      await openLogs(page);
      await clickButtonAndWait(page, "모델 설정 롤백", "model config rolled back", { timeout: 45000 });
      const cleanupButton = page.getByRole("button", { name: "Retention cleanup 실행" }).first();
      await cleanupButton.scrollIntoViewIfNeeded();
      await cleanupButton.click();
      await waitForPanelText(page, "trace retention cleanup executed");
      shots.push(await screenshot(page, "06-mutating-actions"));
      return { actions: ["model_rollback", "retention_cleanup_execute"] };
    });
  }

  await browser.close();

  const report = {
    baseUrl: BASE_URL,
    runId: RUN_ID,
    runMutatingActions: RUN_MUTATING_ACTIONS,
    outputDir: OUT_DIR,
    consoleErrors,
    results,
  };
  fs.writeFileSync(path.join(OUT_DIR, "report.json"), JSON.stringify(report, null, 2), "utf-8");
  const failed = results.filter((result) => result.status !== "passed");
  if (consoleErrors.length || failed.length) {
    console.error(JSON.stringify(report, null, 2));
    process.exit(1);
  }
  console.log(JSON.stringify(report, null, 2));
}

main().catch((error) => {
  console.error(error.stack || error.message || String(error));
  process.exit(1);
});

const { chromium } = require("playwright-core");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8100";
const AGENT_BASE_URL = process.env.AGENT_BASE_URL || "http://127.0.0.1:8101";
const PHR_BASE_URL = process.env.PHR_BASE_URL || "http://127.0.0.1:8102";
const INTERNAL_API_TOKEN = process.env.INTERNAL_API_TOKEN || "playwright-internal-token";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PYTHON = process.env.PYTHON_BIN || "python";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `p1-production-readiness-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

const results = [];
const consoleErrors = [];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function plainText(html) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&quot;/g, '"')
    .replace(/&#34;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
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

async function systemGet(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.text();
}

async function systemGetJson(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function systemPostForm(pathname, values = {}) {
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

async function agentGetJson(pathname) {
  const response = await fetch(`${AGENT_BASE_URL}${pathname}`, {
    headers: { "X-Internal-Api-Token": INTERNAL_API_TOKEN },
  });
  if (!response.ok) {
    throw new Error(`AGENT GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function phrGetJson(pathname) {
  const response = await fetch(`${PHR_BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`PHR GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function chatInfo() {
  const html = await systemGet("/partials/chat-log");
  const countMatch = html.match(/data-chat-message-count="(\d+)"/);
  return {
    count: countMatch ? Number(countMatch[1]) : 0,
    html,
    text: plainText(html),
  };
}

async function capture(page, name, options = {}) {
  const fileName = `${name}.png`;
  await page.screenshot({
    path: path.join(OUT_DIR, fileName),
    fullPage: options.fullPage !== false,
  });
  return fileName;
}

async function runStep(page, id, title, fn) {
  const started = Date.now();
  const screenshots = [];
  try {
    const details = await fn(screenshots);
    results.push({ id, title, status: "passed", elapsedMs: Date.now() - started, details, screenshots });
    console.log(`PASS ${id} ${Date.now() - started}ms`);
  } catch (error) {
    const failureShot = `${id}-failed.png`;
    try {
      await page.screenshot({ path: path.join(OUT_DIR, failureShot), fullPage: true });
      screenshots.push(failureShot);
    } catch (_) {
      // Best-effort failure artifact.
    }
    results.push({ id, title, status: "failed", elapsedMs: Date.now() - started, error: error.message || String(error), screenshots });
    console.error(`FAIL ${id} ${Date.now() - started}ms ${error.message || error}`);
  }
}

async function waitForPageText(page, expected, timeoutMs = 15000) {
  await page.waitForFunction((text) => document.body.innerText.includes(text), expected, { timeout: timeoutMs });
}

async function prepareServicesAndPage(page, screenshots) {
  await systemPostForm("/simulation/reset");
  await systemGetJson("/health");
  await fetch(`${AGENT_BASE_URL}/health`).then((response) => {
    assert(response.ok, `agent health failed:${response.status}`);
  });
  await phrGetJson("/health");
  const readiness = await waitFor(
    "agent_readiness_endpoint",
    async () => {
      const payload = await agentGetJson("/agent/ops/readiness");
      return payload.status !== "critical" ? payload : null;
    },
    30000,
    1000,
  );

  await page.goto(`${BASE_URL}/?scenario=p1-production&run=${RUN_ID}`, { waitUntil: "domcontentloaded", timeout: 20000 });
  await page.waitForSelector("#medications-panel", { timeout: 10000 });
  screenshots.push(await capture(page, "00_initial_home"));
  return { readiness };
}

async function addMedicationViaUi(page, screenshots) {
  await page.locator('input[name="schedule_template"][value="morning_lunch_evening"]').check({ force: true });
  await page.locator('input[name="medication_choice"][value="당뇨약"]').check({ force: true });
  await page.locator('input[name="dosage_choice"][value="1정"]').check({ force: true });
  await page.locator('textarea[name="instructions"]').fill(`P1 Playwright scenario ${RUN_ID}`);
  await page.locator('#medications-panel form[action="/medications"] button[type="submit"]').click();
  await page.waitForLoadState("domcontentloaded", { timeout: 10000 }).catch(() => {});
  await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
  await waitForPageText(page, "당뇨약");
  screenshots.push(await capture(page, "01_medication_added"));
  return { medication: "당뇨약" };
}

async function registerPhrViaUi(page, screenshots) {
  await page.getByRole("button", { name: /설정 완료 및 PHR 등록|PHR 재동기화/ }).click();
  await page.waitForLoadState("domcontentloaded", { timeout: 10000 }).catch(() => {});
  await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
  await waitForPageText(page, "PHR key 발급 완료", 20000);
  screenshots.push(await capture(page, "02_phr_registered"));
  return { phrRegistered: true };
}

async function recordNutritionViaUi(page, screenshots) {
  await page.getByRole("button", { name: /나트륨 초과 점심/ }).click();
  await waitForPageText(page, "짜장면, 탕수육", 15000);
  await waitForPageText(page, "기록됨", 15000);
  const nutritionPanelText = await page.locator("#nutrition-panel").innerText();
  assert(nutritionPanelText.includes("초과") || nutritionPanelText.includes("나트륨"), `nutrition status missing:${nutritionPanelText}`);
  screenshots.push(await capture(page, "03_nutrition_high_sodium"));
  return { meal: "짜장면, 탕수육", panelText: nutritionPanelText.slice(0, 500) };
}

async function submitIntegratedChatViaUi(page, screenshots) {
  await page.locator('button[data-tab-target="chat-page"]').click();
  await page.waitForSelector("#chat-panel", { timeout: 10000 });
  const before = await chatInfo();
  const message = `점심에 짜장면을 먹었고 복약 시간도 같이 봐줘 ${RUN_ID}`;
  await page.locator("#agent-chat-message").fill(message);
  await page.locator('#chat-panel form button[type="submit"]').click();
  await waitFor(
    "chat_user_message_visible",
    async () => {
      const text = await page.locator("#chat-panel").innerText();
      return text.includes(message) ? text : null;
    },
    15000,
    500,
  );
  const finalChat = await waitFor(
    "integrated_chat_async_answer",
    async () => {
      const info = await chatInfo();
      if (info.text.includes("AI 에이전트 오류")) {
        throw new Error(info.text.slice(-700));
      }
      return info.count >= before.count + 2 && info.text.includes("영양과 복약을 함께 보면") ? info : null;
    },
    90000,
    1000,
  );
  await page.goto(`${BASE_URL}/?scenario=p1-production-final&run=${RUN_ID}#chat`, { waitUntil: "domcontentloaded", timeout: 20000 });
  await page.locator('button[data-tab-target="chat-page"]').click();
  await page.waitForSelector("#chat-panel", { timeout: 10000 });
  await waitForPageText(page, "영양과 복약을 함께 보면", 15000);
  screenshots.push(await capture(page, "04_integrated_chat_answer"));
  return { beforeCount: before.count, afterCount: finalChat.count, message };
}

async function verifyReadinessAndLoadProbe(_page, screenshots) {
  const readiness = await waitFor(
    "agent_readiness_after_chat",
    async () => {
      const payload = await agentGetJson("/agent/ops/readiness");
      const deadCount = Number(payload.metrics && payload.metrics.dead_count ? payload.metrics.dead_count : 0);
      return deadCount === 0 ? payload : null;
    },
    60000,
    1000,
  );
  const probe = spawnSync(
    PYTHON,
    [
      "tools/p1_load_probe.py",
      "--system-base-url",
      BASE_URL,
      "--agent-base-url",
      AGENT_BASE_URL,
      "--phr-base-url",
      PHR_BASE_URL,
      "--internal-api-token",
      INTERNAL_API_TOKEN,
      "--concurrency",
      "6",
      "--iterations",
      "24",
      "--timeout",
      "5",
    ],
    {
      cwd: process.cwd(),
      encoding: "utf8",
      env: process.env,
    },
  );
  fs.writeFileSync(path.join(OUT_DIR, "p1-load-probe.stdout.json"), probe.stdout || "", "utf8");
  fs.writeFileSync(path.join(OUT_DIR, "p1-load-probe.stderr.log"), probe.stderr || "", "utf8");
  assert(probe.status === 0, `p1_load_probe exited ${probe.status}:${probe.stderr || probe.stdout}`);
  const probePayload = JSON.parse(probe.stdout);
  assert(probePayload.status === "ok", `p1 load probe not ok:${probe.stdout}`);
  screenshots.push("p1-load-probe.stdout.json");
  return { readiness, probe: probePayload };
}

function writeReport() {
  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    agentBaseUrl: AGENT_BASE_URL,
    phrBaseUrl: PHR_BASE_URL,
    outputDir: OUT_DIR,
    results,
    consoleErrors,
    passed: results.filter((result) => result.status === "passed").length,
    failed: results.filter((result) => result.status === "failed").length,
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");

  const lines = [];
  lines.push("# P1 Production Readiness Playwright Report");
  lines.push("");
  lines.push(`- Run ID: ${RUN_ID}`);
  lines.push(`- Base URL: ${BASE_URL}`);
  lines.push(`- Agent URL: ${AGENT_BASE_URL}`);
  lines.push(`- PHR URL: ${PHR_BASE_URL}`);
  lines.push(`- Passed: ${summary.passed}`);
  lines.push(`- Failed: ${summary.failed}`);
  lines.push(`- Console errors: ${consoleErrors.length}`);
  lines.push("");
  lines.push("| Step | Status | Elapsed | Screenshots |");
  lines.push("| --- | --- | ---: | --- |");
  for (const result of results) {
    lines.push(`| ${result.id} ${result.title} | ${result.status} | ${result.elapsedMs}ms | ${(result.screenshots || []).join(", ")} |`);
  }
  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
  console.log(JSON.stringify(summary, null, 2));
  if (summary.failed > 0 || consoleErrors.length > 0) {
    process.exitCode = 1;
  }
}

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    consoleErrors.push(error.message || String(error));
  });

  try {
    await runStep(page, "00", "서비스와 초기 화면 확인", (screenshots) => prepareServicesAndPage(page, screenshots));
    await runStep(page, "01", "UI로 복약 일정 추가", (screenshots) => addMedicationViaUi(page, screenshots));
    await runStep(page, "02", "UI로 PHR 등록", (screenshots) => registerPhrViaUi(page, screenshots));
    await runStep(page, "03", "UI로 고나트륨 식사 시나리오 기록", (screenshots) => recordNutritionViaUi(page, screenshots));
    await runStep(page, "04", "영양+복약 통합 채팅 async 완료 확인", (screenshots) => submitIntegratedChatViaUi(page, screenshots));
    await runStep(page, "05", "agent readiness와 P1 load probe 확인", (screenshots) => verifyReadinessAndLoadProbe(page, screenshots));
  } finally {
    await browser.close();
    writeReport();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

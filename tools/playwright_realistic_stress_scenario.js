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
const OUT_DIR = path.join("outputs", "playwright", `realistic-stress-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

const results = [];
const browserEvents = [];

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

async function waitFor(name, predicate, timeoutMs = 90000, intervalMs = 500) {
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

async function getText(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.text();
}

async function getJson(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
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

async function agentJson(pathname) {
  const response = await fetch(`${AGENT_BASE_URL}${pathname}`, {
    headers: { "X-Internal-Api-Token": INTERNAL_API_TOKEN },
  });
  if (!response.ok) {
    throw new Error(`AGENT GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

async function serviceHealth(url, label) {
  const response = await fetch(`${url}/health`);
  assert(response.ok, `${label} health failed:${response.status}`);
  return response.json();
}

async function chatInfo() {
  const html = await getText("/partials/chat-log");
  const countMatch = html.match(/data-chat-message-count="(\d+)"/);
  return {
    count: countMatch ? Number(countMatch[1]) : 0,
    html,
    text: plainText(html),
  };
}

async function capture(page, name, fullPage = true) {
  const fileName = `${name}.png`;
  await page.screenshot({ path: path.join(OUT_DIR, fileName), fullPage });
  return fileName;
}

function trackPage(page, label) {
  page.on("console", (message) => {
    if (message.type() === "error") {
      browserEvents.push({ label, type: "console_error", text: message.text() });
    }
  });
  page.on("pageerror", (error) => {
    browserEvents.push({ label, type: "page_error", text: error.message || String(error) });
  });
  page.on("requestfailed", (request) => {
    const url = request.url();
    const failure = request.failure() ? request.failure().errorText : "";
    if (url.includes("/favicon") || failure.includes("ERR_ABORTED")) {
      return;
    }
    browserEvents.push({ label, type: "request_failed", text: `${request.method()} ${url} ${failure}`.trim() });
  });
}

async function runStep(id, title, fn) {
  const started = Date.now();
  const screenshots = [];
  try {
    const details = await fn(screenshots);
    const elapsedMs = Date.now() - started;
    results.push({ id, title, status: "passed", elapsedMs, details, screenshots });
    console.log(`PASS ${id} ${elapsedMs}ms`);
  } catch (error) {
    const elapsedMs = Date.now() - started;
    results.push({ id, title, status: "failed", elapsedMs, error: error.message || String(error), screenshots });
    console.error(`FAIL ${id} ${elapsedMs}ms ${error.message || error}`);
  }
}

async function openHome(page, tag) {
  await page.goto(`${BASE_URL}/?stress=${tag}&run=${RUN_ID}`, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForSelector("#medications-panel", { timeout: 15000 });
}

async function openChat(page, tag) {
  await page.goto(`${BASE_URL}/?stress=${tag}&run=${RUN_ID}#chat`, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.locator('button[data-tab-target="chat-page"]').click();
  await page.waitForSelector("#chat-panel", { timeout: 15000 });
}

async function waitForBodyText(page, text, timeoutMs = 20000) {
  await page.waitForFunction((expected) => document.body.innerText.includes(expected), text, { timeout: timeoutMs });
}

async function addMedicationAndRegisterPhr(page, screenshots) {
  await page.locator('input[name="schedule_template"][value="morning_lunch_evening"]').check({ force: true });
  await page.locator('input[name="medication_choice"][value="당뇨약"]').check({ force: true });
  await page.locator('input[name="dosage_choice"][value="1정"]').check({ force: true });
  await page.locator('textarea[name="instructions"]').fill(`realistic stress ${RUN_ID}: 식사/복약 통합 장기 문구 안정성 확인`);
  await page.locator('#medications-panel form[action="/medications"] button[type="submit"]').click();
  await waitForBodyText(page, "당뇨약", 20000);
  await page.getByRole("button", { name: /설정 완료 및 PHR 등록|PHR 재동기화/ }).click();
  await waitForBodyText(page, "PHR key 발급 완료", 30000);
  screenshots.push(await capture(page, "01_medication_phr_desktop"));
}

async function nutritionDailySummary() {
  const payload = await getJson("/api/nutrition/daily-summary");
  return payload.daily_summary;
}

async function clickNutritionScenario(page, label) {
  await page.getByRole("button", { name: new RegExp(label) }).click({ timeout: 15000 });
}

async function submitChatInPage(page, message) {
  await openChat(page, `chat-${encodeURIComponent(message.slice(0, 16))}`);
  await page.locator("#agent-chat-message").fill(message);
  await page.locator('#chat-panel form button[type="submit"]').click();
}

async function submitChatViaFetch(page, message) {
  const status = await page.evaluate(
    async ({ baseUrl, text }) => {
      const response = await fetch(`${baseUrl}/chat/system`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ event_type: "multiturn_chat", message: text }),
      });
      return response.status;
    },
    { baseUrl: BASE_URL, text: message },
  );
  assert(status >= 200 && status < 300, `chat fetch submit failed:${status}`);
}

async function assertNoCriticalBrowserEvents() {
  const relevant = browserEvents.filter((event) => {
    if (event.text.includes("ResizeObserver loop")) {
      return false;
    }
    return true;
  });
  assert(relevant.length === 0, `browser events found:${JSON.stringify(relevant, null, 2)}`);
}

function runLoadProbe() {
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
      "8",
      "--iterations",
      "32",
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
  const payload = JSON.parse(probe.stdout);
  assert(payload.status === "ok", `p1 load probe not ok:${probe.stdout}`);
  return payload;
}

function writeReport() {
  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    agentBaseUrl: AGENT_BASE_URL,
    phrBaseUrl: PHR_BASE_URL,
    outputDir: OUT_DIR,
    results,
    browserEvents,
    passed: results.filter((result) => result.status === "passed").length,
    failed: results.filter((result) => result.status === "failed").length,
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");

  const lines = [
    "# Realistic Stress Playwright Report",
    "",
    `- Run ID: ${RUN_ID}`,
    `- Base URL: ${BASE_URL}`,
    `- Agent URL: ${AGENT_BASE_URL}`,
    `- PHR URL: ${PHR_BASE_URL}`,
    `- Passed: ${summary.passed}`,
    `- Failed: ${summary.failed}`,
    `- Browser events: ${browserEvents.length}`,
    "",
    "| Step | Status | Elapsed | Artifacts |",
    "| --- | --- | ---: | --- |",
  ];
  for (const result of results) {
    lines.push(`| ${result.id} ${result.title} | ${result.status} | ${result.elapsedMs}ms | ${(result.screenshots || []).join(", ")} |`);
  }
  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
  console.log(JSON.stringify(summary, null, 2));
  if (summary.failed > 0 || browserEvents.length > 0) {
    process.exitCode = 1;
  }
}

async function main() {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const desktop = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const mobile = await browser.newPage({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    userAgent:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
  });
  const background = await browser.newPage({ viewport: { width: 1024, height: 768 } });
  trackPage(desktop, "desktop");
  trackPage(mobile, "mobile");
  trackPage(background, "background");

  try {
    await runStep("00", "격리 서비스 health/readiness와 양쪽 화면 로드", async (screenshots) => {
      await postForm("/simulation/reset");
      const health = {
        system: await getJson("/health"),
        agent: await serviceHealth(AGENT_BASE_URL, "agent"),
        phr: await serviceHealth(PHR_BASE_URL, "phr"),
      };
      const readiness = await waitFor(
        "initial_readiness",
        async () => {
          const payload = await agentJson("/agent/ops/readiness");
          return payload.status !== "critical" ? payload : null;
        },
        30000,
        1000,
      );
      await openHome(desktop, "desktop-initial");
      await openHome(mobile, "mobile-initial");
      await openHome(background, "background-initial");
      screenshots.push(await capture(desktop, "00_desktop_initial"));
      screenshots.push(await capture(mobile, "00_mobile_initial"));
      return { health, readinessStatus: readiness.status };
    });

    await runStep("01", "데스크톱 복약 등록 후 PHR 등록, 모바일 상태 동기화", async (screenshots) => {
      await addMedicationAndRegisterPhr(desktop, screenshots);
      await openHome(mobile, "mobile-after-phr");
      await waitForBodyText(mobile, "당뇨약", 20000);
      await waitForBodyText(mobile, "PHR key 발급 완료", 20000);
      screenshots.push(await capture(mobile, "01_mobile_phr_synced"));
      return { medication: "당뇨약", phrSyncedOnMobile: true };
    });

    await runStep("02", "동일 영양 시나리오 동시 클릭은 중복 식사로 늘지 않는다", async (screenshots) => {
      await Promise.allSettled([clickNutritionScenario(desktop, "나트륨 초과 점심"), clickNutritionScenario(mobile, "나트륨 초과 점심")]);
      const duplicateSummary = await waitFor("duplicate_high_sodium_collapsed", async () => {
        const summary = await nutritionDailySummary();
        return Number(summary.total_meals) === 1 ? summary : null;
      });
      await openHome(desktop, "after-duplicate-nutrition");
      await waitForBodyText(desktop, "짜장면, 탕수육", 20000);
      screenshots.push(await capture(desktop, "02_duplicate_nutrition_collapsed"));
      return { totalMeals: duplicateSummary.total_meals, status: duplicateSummary.summary.status };
    });

    await runStep("03", "아침/저녁 추가 후 일일 영양 초과와 UI 유지 확인", async (screenshots) => {
      await clickNutritionScenario(desktop, "정상 아침");
      await openHome(mobile, "mobile-after-breakfast");
      await clickNutritionScenario(mobile, "칼로리/지방 초과 저녁");
      const summary = await waitFor("three_meals_recorded", async () => {
        const payload = await nutritionDailySummary();
        return Number(payload.total_meals) === 3 ? payload : null;
      });
      await openHome(desktop, "desktop-three-meals");
      await waitForBodyText(desktop, "식사 3건", 20000);
      await waitForBodyText(desktop, "콤비네이션피자, 콜라", 20000);
      screenshots.push(await capture(desktop, "03_desktop_three_meals"));
      screenshots.push(await capture(mobile, "03_mobile_three_meals"));
      return {
        totalMeals: summary.total_meals,
        exceededNutrients: summary.summary.exceeded_nutrients,
      };
    });

    await runStep("04", "두 세션과 fetch가 동시에 보낸 채팅 3건을 모두 async 완료", async (screenshots) => {
      const before = await chatInfo();
      const messages = [
        `아침은 현미밥, 점심은 짜장면이었고 복약 시간도 같이 봐줘 ${RUN_ID}-A`,
        `저녁 피자까지 먹었는데 오늘 나트륨과 당뇨약 복용을 같이 점검해줘 ${RUN_ID}-B`,
        `오늘 식사 3끼와 처방된 약 시간을 한 번에 정리해줘 ${RUN_ID}-C`,
      ];
      await Promise.all([
        submitChatInPage(desktop, messages[0]),
        submitChatInPage(mobile, messages[1]),
        submitChatViaFetch(background, messages[2]),
      ]);
      const finalChat = await waitFor(
        "three_concurrent_chat_answers",
        async () => {
          const info = await chatInfo();
          if (info.text.includes("AI 에이전트 오류") || info.text.includes("AI가 대화를 처리하지 못했습니다")) {
            throw new Error(info.text.slice(-1000));
          }
          const allUserMessagesVisible = messages.every((message) => info.text.includes(message));
          const integratedAnswerCount = (info.text.match(/영양과 복약을 함께 보면/g) || []).length;
          return allUserMessagesVisible && info.count >= before.count + 6 && integratedAnswerCount >= 3 ? info : null;
        },
        120000,
        1000,
      );
      await openChat(desktop, "desktop-chat-final");
      await openChat(mobile, "mobile-chat-final");
      screenshots.push(await capture(desktop, "04_desktop_concurrent_chat"));
      screenshots.push(await capture(mobile, "04_mobile_concurrent_chat"));
      return { beforeCount: before.count, afterCount: finalChat.count, messages };
    });

    await runStep("05", "새로고침 후 상태 유지와 가로 overflow 점검", async (screenshots) => {
      await openHome(desktop, "desktop-reload-check");
      await openHome(mobile, "mobile-reload-check");
      await waitForBodyText(desktop, "식사 3건", 20000);
      await waitForBodyText(mobile, "식사 3건", 20000);
      await waitForBodyText(desktop, "당뇨약", 20000);
      await waitForBodyText(mobile, "당뇨약", 20000);
      const desktopOverflow = await desktop.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      const mobileOverflow = await mobile.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert(desktopOverflow <= 2, `desktop horizontal overflow:${desktopOverflow}`);
      assert(mobileOverflow <= 2, `mobile horizontal overflow:${mobileOverflow}`);
      screenshots.push(await capture(desktop, "05_desktop_reload"));
      screenshots.push(await capture(mobile, "05_mobile_reload"));
      return { desktopOverflow, mobileOverflow };
    });

    await runStep("06", "Agent readiness와 P1 부하 예산 확인", async (screenshots) => {
      const readiness = await waitFor(
        "final_agent_readiness",
        async () => {
          const payload = await agentJson("/agent/ops/readiness");
          const deadCount = Number(payload.metrics && payload.metrics.dead_count ? payload.metrics.dead_count : 0);
          return deadCount === 0 && payload.status !== "critical" ? payload : null;
        },
        60000,
        1000,
      );
      const probe = runLoadProbe();
      screenshots.push("p1-load-probe.stdout.json");
      return {
        readinessStatus: readiness.status,
        readinessMetrics: readiness.metrics,
        probeSummary: probe.summary,
        probeAlerts: probe.alerts,
      };
    });

    await runStep("07", "브라우저 콘솔/요청 실패 이벤트 확인", async () => {
      await assertNoCriticalBrowserEvents();
      return { browserEventCount: browserEvents.length };
    });
  } finally {
    await browser.close();
    writeReport();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

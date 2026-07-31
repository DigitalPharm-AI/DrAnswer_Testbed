const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const artifactDir =
  process.env.BROWSER_ARTIFACT_DIR || path.join("artifacts", "browser-ci");
const applicationOrigin = new URL(baseUrl).origin;

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function chromeExecutable() {
  const candidates = [
    process.env.CHROME_PATH,
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  const executable = candidates.find((candidate) => fs.existsSync(candidate));
  if (!executable) {
    throw new Error(`chrome_executable_not_found:${candidates.join(",")}`);
  }
  return executable;
}

async function waitForInitialBffReads(page, responses) {
  const requiredPaths = [
    "/api/ui/v1/dashboard",
    "/api/ui/v1/medication-scenarios",
    "/api/ui/v1/chat/history",
    "/api/ui/v1/status",
  ];
  const deadline = Date.now() + 15_000;

  while (
    Date.now() < deadline &&
    requiredPaths.some((pathname) => !responses.has(pathname))
  ) {
    await page.waitForTimeout(100);
  }

  for (const pathname of requiredPaths) {
    const status = responses.get(pathname);
    assert(status !== undefined, `initial BFF request was not observed: ${pathname}`);
    assert(status === 200, `initial BFF request failed: ${pathname} -> ${status}`);
  }
}

async function assertReactRoot(page) {
  const navigation = await page.goto(`${baseUrl}/`, {
    waitUntil: "domcontentloaded",
    timeout: 30_000,
  });
  assert(navigation?.status() === 200, `root navigation failed: ${navigation?.status()}`);

  await page.locator("#root").waitFor({ state: "visible" });
  await page
    .getByRole("heading", {
      name: "닥터앤서 AI 복약 케어 테스트",
      level: 1,
    })
    .waitFor({ state: "visible" });
  await page.getByRole("tab", { name: "HOME", exact: true }).waitFor({
    state: "visible",
  });
  await page.getByRole("tab", { name: "Chat", exact: true }).waitFor({
    state: "visible",
  });
  await page.locator('[aria-label="서버 상태"]').waitFor({ state: "visible" });
  await page.getByRole("heading", { name: "오늘 복약 일정" }).waitFor({
    state: "visible",
  });
}

async function assertReadOnlyReactNavigation(page) {
  await page.getByRole("tab", { name: "Chat", exact: true }).click();
  await page.getByRole("heading", { name: "에이전트와의 대화" }).waitFor({
    state: "visible",
  });
  await page
    .getByRole("textbox", { name: "AI 에이전트에게 질문하기" })
    .waitFor({ state: "visible" });

  await page.getByRole("tab", { name: "HOME", exact: true }).click();
  await page.getByRole("heading", { name: "오늘 복약 일정" }).waitFor({
    state: "visible",
  });
}

async function assertLegacySurfaceIsAbsent(page) {
  const legacySelectors = [
    "#medications-panel",
    "#chat-panel",
    "#logs-panel",
    "[hx-get]",
    "[hx-post]",
    "script[src*='htmx']",
  ];
  for (const selector of legacySelectors) {
    assert(
      (await page.locator(selector).count()) === 0,
      `legacy DOM selector is still present: ${selector}`,
    );
  }

  const localAssets = await page
    .locator("script[src], link[rel='stylesheet'][href]")
    .evaluateAll((elements, origin) =>
      elements
        .map((element) => element.src || element.href)
        .filter((value) => value && new URL(value).origin === origin)
        .map((value) => new URL(value).pathname),
    applicationOrigin);
  assert(localAssets.length >= 2, "React JavaScript and stylesheet assets were not found");
  assert(
    localAssets.every((pathname) => pathname.startsWith("/static/react/")),
    `non-React local assets were loaded: ${JSON.stringify(localAssets)}`,
  );

  const removedPaths = [
    "/react",
    "/static/styles.css",
    "/static/htmx-lite.js",
    "/static/notifications.js",
  ];
  for (const pathname of removedPaths) {
    const response = await page.request.get(`${baseUrl}${pathname}`);
    assert(
      response.status() === 404,
      `removed legacy path is still exposed: ${pathname} -> ${response.status()}`,
    );
  }
}

async function main() {
  fs.mkdirSync(artifactDir, { recursive: true });
  const browser = await chromium.launch({
    executablePath: chromeExecutable(),
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const consoleErrors = [];
  const unsafeRequests = [];
  const initialBffResponses = new Map();

  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    consoleErrors.push(error.message);
  });
  page.on("request", (request) => {
    const requestUrl = new URL(request.url());
    if (
      requestUrl.origin === applicationOrigin &&
      !["GET", "HEAD", "OPTIONS"].includes(request.method())
    ) {
      unsafeRequests.push(`${request.method()} ${requestUrl.pathname}`);
    }
  });
  page.on("response", (response) => {
    const responseUrl = new URL(response.url());
    if (
      responseUrl.origin === applicationOrigin &&
      responseUrl.pathname.startsWith("/api/ui/v1/")
    ) {
      initialBffResponses.set(responseUrl.pathname, response.status());
    }
  });

  try {
    await assertReactRoot(page);
    await waitForInitialBffReads(page, initialBffResponses);
    await assertReadOnlyReactNavigation(page);
    await assertLegacySurfaceIsAbsent(page);

    assert(
      unsafeRequests.length === 0,
      `read-only smoke emitted mutation requests: ${JSON.stringify(unsafeRequests)}`,
    );
    assert(
      consoleErrors.length === 0,
      `browser console errors: ${JSON.stringify(consoleErrors)}`,
    );
    console.log(
      "PASS browser CI: React root, BFF read-only bootstrap, client navigation, legacy surface absent",
    );
  } catch (error) {
    await page
      .screenshot({
        path: path.join(artifactDir, "failure.png"),
        fullPage: true,
      })
      .catch(() => {});
    throw error;
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

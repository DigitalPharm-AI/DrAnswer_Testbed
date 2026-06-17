const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `suppression-ui-repro-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

async function postForm(pathname, values = {}) {
  const response = await fetch(`${BASE_URL}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(values),
  });
  if (!response.ok && response.status !== 204) {
    throw new Error(`POST ${pathname} failed: ${response.status} ${await response.text()}`);
  }
}

async function main() {
  await postForm("/simulation/reset");
  await postForm("/reminders/suppression", { suppressed: "true" });

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  const requests = [];
  const responses = [];
  const consoleMessages = [];

  page.on("request", (request) => {
    if (request.url().includes("/reminders/suppression")) {
      requests.push({ method: request.method(), url: request.url(), postData: request.postData() });
    }
  });
  page.on("response", (response) => {
    if (response.url().includes("/reminders/suppression")) {
      responses.push({ status: response.status(), url: response.url() });
    }
  });
  page.on("console", (message) => {
    consoleMessages.push({ type: message.type(), text: message.text() });
  });
  page.on("pageerror", (error) => {
    consoleMessages.push({ type: "pageerror", text: error.message });
  });

  try {
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.getByText("알림 전체 켜기", { exact: false }).first().waitFor({ timeout: 30000 });
    await page.waitForTimeout(Number(process.env.SUPPRESSION_UI_CLICK_DELAY_MS || "0"));
    await page.getByRole("button", { name: "알림 전체 켜기" }).click();
    await page.getByText("알림 전체 끄기", { exact: false }).first().waitFor({ timeout: 10000 });
    await page.screenshot({ path: path.join(OUT_DIR, "passed.png"), fullPage: true });
  } catch (error) {
    await page.screenshot({ path: path.join(OUT_DIR, "failed.png"), fullPage: true }).catch(() => {});
    fs.writeFileSync(
      path.join(OUT_DIR, "debug.json"),
      JSON.stringify({ error: error.message || String(error), requests, responses, consoleMessages }, null, 2),
      "utf8",
    );
    throw error;
  } finally {
    await browser.close();
  }

  const summary = { status: "passed", outputDir: OUT_DIR, requests, responses, consoleMessages };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  console.log(JSON.stringify(summary, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

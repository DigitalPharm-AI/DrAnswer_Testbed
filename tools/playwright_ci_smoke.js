const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const artifactDir = process.env.BROWSER_ARTIFACT_DIR || path.join("artifacts", "browser-ci");

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

async function assertMedicationErrorPreservesDom(page) {
  const form = page.locator("#medications-panel form[action='/medications']");
  const panel = page.locator("#medications-panel");
  const submit = form.locator("button[type='submit']");
  const instruction = `브라우저 오류 보존-${Date.now()}`;

  await panel.evaluate((element) => {
    element.dataset.browserCiMarker = "preserve-on-error";
  });
  await form.locator("textarea[name='instructions']").fill(instruction);
  await form.locator("input[name='submission_id']").evaluate((input) => {
    input.value = "invalid-browser-submission-id";
  });

  await page.route("**/medications", async (route) => {
    if (route.request().method() === "POST") {
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    await route.continue();
  });

  const responsePromise = page.waitForResponse(
    (response) =>
      response.url() === `${baseUrl}/medications` &&
      response.request().method() === "POST",
  );
  await submit.click();
  await page.waitForFunction(
    () =>
      document.querySelector("#medications-panel form[action='/medications'] button[type='submit']")
        ?.disabled === true,
  );
  const response = await responsePromise;
  assert(response.status() === 422, `expected medication 422, received ${response.status()}`);
  await page.waitForFunction(
    () =>
      document.querySelector("#medications-panel form[action='/medications'] button[type='submit']")
        ?.disabled === false,
  );

  assert(
    (await panel.getAttribute("data-browser-ci-marker")) === "preserve-on-error",
    "medication panel was replaced after a 4xx response",
  );
  assert(
    (await form.locator("textarea[name='instructions']").inputValue()) === instruction,
    "medication draft was lost after a 4xx response",
  );
  assert(await page.locator("#app-request-feedback:not([hidden])").isVisible(), "4xx feedback was not displayed");
}

async function assertMedicationSubmissionIsIdempotent(page) {
  const token = `브라우저-멱등-${Date.now()}`;
  const result = await page.evaluate(async ({ token }) => {
    const form = document.querySelector("#medications-panel form[action='/medications']");
    if (!(form instanceof HTMLFormElement)) {
      throw new Error("medication form not found");
    }
    form.querySelector("input[name='submission_id']").value = crypto.randomUUID();
    form.querySelector("textarea[name='instructions']").value = token;
    const encoded = new URLSearchParams(new FormData(form)).toString();
    const request = () =>
      fetch("/medications", {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "HX-Request": "true",
        },
        body: encoded,
      });
    const responses = await Promise.all([request(), request()]);
    const dashboard = await fetch("/", {
      headers: { "HX-Request": "true" },
    });
    const html = await dashboard.text();
    const documentCopy = new DOMParser().parseFromString(html, "text/html");
    const matchingCards = Array.from(documentCopy.querySelectorAll(".med-card")).filter((card) =>
      card.textContent.includes(token),
    );
    return {
      statuses: responses.map((response) => response.status),
      count: matchingCards.length,
      cardTexts: Array.from(documentCopy.querySelectorAll(".med-card")).map((card) =>
        card.textContent.trim().replace(/\s+/g, " "),
      ),
    };
  }, { token });

  assert(result.statuses.every((status) => status === 204), `duplicate submission statuses: ${result.statuses}`);
  assert(
    result.count === 1,
    `duplicate submission created ${result.count} matching medication plans: ${JSON.stringify(result.cardTexts)}`,
  );
}

async function assertChatDraftSurvivesRefresh(page) {
  await page.locator(".app-tab[data-tab-target='chat-page']").click();
  const composer = page.locator("#agent-chat-message");
  const draft = `채팅 초안 보존-${Date.now()}`;
  await composer.fill(draft);
  await composer.focus();
  await page.waitForTimeout(3500);
  assert((await page.locator("#agent-chat-message").inputValue()) === draft, "chat draft was lost during panel refresh");
}

async function assertTimelineDateSurvivesRefresh(page) {
  await page.locator(".app-tab[data-tab-target='home-page']").click();
  const panel = page.locator("#timeline-panel");
  const currentDate = await panel.getAttribute("data-timeline-date");
  const links = page.locator("#timeline-panel a[hx-get^='/partials/timeline?timeline_date=']");
  const linkCount = await links.count();
  let selectedDate = "";
  let selectedLink = null;

  for (let index = 0; index < linkCount; index += 1) {
    const link = links.nth(index);
    const hxGet = await link.getAttribute("hx-get");
    const candidate = new URL(hxGet, baseUrl).searchParams.get("timeline_date");
    if (candidate && candidate !== currentDate) {
      selectedDate = candidate;
      selectedLink = link;
      break;
    }
  }
  assert(selectedLink, "a non-current timeline date was not available");

  await selectedLink.click();
  await page.waitForFunction(
    (expected) => document.querySelector("#timeline-panel")?.dataset.timelineDate === expected,
    selectedDate,
  );
  await page.waitForTimeout(3500);
  assert(
    (await page.locator("#timeline-panel").getAttribute("data-timeline-date")) === selectedDate,
    "timeline date was reset during periodic refresh",
  );
}

async function main() {
  fs.mkdirSync(artifactDir, { recursive: true });
  const browser = await chromium.launch({
    executablePath: chromeExecutable(),
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  try {
    await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 30_000 });
    await page.locator("#medications-panel").waitFor({ state: "visible" });
    await assertMedicationErrorPreservesDom(page);
    await assertMedicationSubmissionIsIdempotent(page);
    await assertChatDraftSurvivesRefresh(page);
    await assertTimelineDateSurvivesRefresh(page);
    console.log("PASS browser CI: 4xx preservation, busy recovery, idempotency, chat draft, timeline date");
  } catch (error) {
    await page.screenshot({ path: path.join(artifactDir, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

const { chromium } = require("playwright-core");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8100";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PYTHON = process.env.PYTHON_BIN || path.join(".venv", "Scripts", "python.exe");
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `persona-policy-challenge-lab-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });
const results = [];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function getJson(pathname) {
  const response = await fetch(`${BASE_URL}${pathname}`);
  if (!response.ok) {
    throw new Error(`GET ${pathname} failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

function fixture(command, options = {}) {
  const args = ["tools/playwright_adherence_pattern_fixture.py", command];
  if (options.scenario) {
    args.push(options.scenario);
  }
  for (const [key, value] of Object.entries(options.flags || {})) {
    args.push(`--${key}`);
    args.push(String(value));
  }
  const result = spawnSync(PYTHON, args, {
    cwd: process.cwd(),
    encoding: "utf8",
    env: process.env,
  });
  if (result.status !== 0) {
    throw new Error(`fixture_${command}_failed:${result.stderr || result.stdout}`);
  }
  const stdout = (result.stdout || "").trim();
  return stdout ? JSON.parse(stdout) : {};
}

function seedRound(pattern, day) {
  return fixture("persona-seed-round", {
    flags: {
      pattern,
      "simulation-day": day,
    },
  });
}

function recordReaction(personaId, action, success, day) {
  return fixture("persona-record-reaction", {
    flags: {
      "persona-id": personaId,
      action,
      success,
      "simulation-day": day,
    },
  });
}

async function capture(page, name) {
  await page.goto(`${BASE_URL}/?challenge=${encodeURIComponent(name)}#chat`, {
    waitUntil: "domcontentloaded",
    timeout: 20000,
  });
  const screenshot = `${name}.png`;
  await page.screenshot({ path: path.join(OUT_DIR, screenshot), fullPage: true });
  return screenshot;
}

async function runExpectedFailure(page, scenario) {
  const started = Date.now();
  const screenshots = [];
  try {
    const details = await scenario.run(page, screenshots);
    results.push({
      id: scenario.id,
      title: scenario.title,
      status: "unexpected_pass",
      elapsedMs: Date.now() - started,
      details,
      screenshots,
    });
    console.error(`UNEXPECTED_PASS ${scenario.id} ${Date.now() - started}ms`);
  } catch (error) {
    results.push({
      id: scenario.id,
      title: scenario.title,
      status: "expected_failed",
      elapsedMs: Date.now() - started,
      knownGap: scenario.knownGap,
      failure: error.message || String(error),
      screenshots,
    });
    console.log(`EXPECTED_FAIL ${scenario.id} ${Date.now() - started}ms ${error.message || error}`);
  }
}

const scenarios = [
  {
    id: "CROSS_PERSONA_TONE_REUSE",
    title: "다른 페르소나 take 성공 이력이 다음 페르소나 tone을 오염시키면 안 됨",
    knownGap: "tone policy는 persona_id를 입력받지 않아 최근 take 성공 tone을 페르소나별로 분리하지 못한다.",
    run: async (page, screenshots) => {
      fixture("persona-reset");
      seedRound("B", 1);
      recordReaction("lazy_warning_responder", "ignore", false, 1);
      seedRound("B", 2);
      recordReaction("lazy_warning_responder", "ignore", false, 2);
      seedRound("B", 3);
      recordReaction("lazy_warning_responder", "snooze", false, 3);
      seedRound("B", 4);
      recordReaction("lazy_warning_responder", "snooze", false, 4);
      const lazyWarning = seedRound("B", 5);
      assert(lazyWarning.tone_key === "warning_soft", `fixture did not reach warning_soft:${JSON.stringify(lazyWarning)}`);
      recordReaction("lazy_warning_responder", "take", true, 5);

      const busyFirst = seedRound("B", 6);
      screenshots.push(await capture(page, "cross-persona-tone-reuse"));
      assert(
        busyFirst.tone_key !== "warning_soft",
        `cross_persona_contamination: busy persona got ${busyFirst.tone_key} because lazy persona history leaked:${JSON.stringify(busyFirst.tone_policy)}`,
      );
      return { lazyWarning, busyFirst };
    },
  },
];

function writeReport(summary) {
  const lines = [];
  lines.push("# Persona Policy Challenge Lab");
  lines.push("");
  lines.push(`- Run ID: ${RUN_ID}`);
  lines.push(`- Base URL: ${BASE_URL}`);
  lines.push(`- Expected failures detected: ${summary.expectedFailed}`);
  lines.push(`- Unexpected passes: ${summary.unexpectedPassed}`);
  lines.push("");
  for (const result of summary.results) {
    lines.push(`## ${result.id}`);
    lines.push("");
    lines.push(`- Title: ${result.title}`);
    lines.push(`- Status: ${result.status}`);
    lines.push(`- Elapsed: ${result.elapsedMs}ms`);
    if (result.knownGap) {
      lines.push(`- Known gap: ${result.knownGap}`);
    }
    if (result.failure) {
      lines.push(`- Failure: ${result.failure}`);
    }
    if (result.screenshots && result.screenshots.length) {
      lines.push(`- Screenshots: ${result.screenshots.map((name) => path.join(OUT_DIR, name)).join(", ")}`);
    }
    lines.push("");
  }
  fs.writeFileSync(path.join(OUT_DIR, "report.md"), `${lines.join("\n")}\n`, "utf8");
}

async function main() {
  await getJson("/health");
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  try {
    for (const scenario of scenarios) {
      await runExpectedFailure(page, scenario);
    }
  } finally {
    await browser.close();
  }

  const summary = {
    runId: RUN_ID,
    baseUrl: BASE_URL,
    outputDir: OUT_DIR,
    results,
    expectedFailed: results.filter((result) => result.status === "expected_failed").length,
    unexpectedPassed: results.filter((result) => result.status === "unexpected_pass").length,
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  writeReport(summary);
  console.log(JSON.stringify(summary, null, 2));
  if (summary.unexpectedPassed > 0 || summary.expectedFailed !== scenarios.length) {
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

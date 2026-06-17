const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:8000";
const CHROME = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PYTHON = process.env.PYTHON_BIN || "python";
const SYSTEM_DB_PATH = process.env.SYSTEM_DB_PATH || "";
const RUN_ID = Date.now();
const OUT_DIR = path.join("outputs", "playwright", `real-llm-generated-missed-dose-${RUN_ID}`);

fs.mkdirSync(OUT_DIR, { recursive: true });

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
}

async function waitFor(name, predicate, timeoutMs = 180000, intervalMs = 1500) {
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

function queryLatestMissedDoseChat() {
  if (!SYSTEM_DB_PATH) {
    throw new Error("SYSTEM_DB_PATH was not provided by the runner.");
  }
  const code = `
import json
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.row_factory = sqlite3.Row
row = conn.execute(
    "select id, content, metadata_json, created_at from chat_messages where category = 'missed_dose' order by id desc limit 1"
).fetchone()
if row is None:
    print("{}")
else:
    data = dict(row)
    try:
        data["metadata"] = json.loads(data.get("metadata_json") or "{}")
    except Exception:
        data["metadata"] = {}
    print(json.dumps(data, ensure_ascii=False))
`;
  const raw = execFileSync(PYTHON, ["-c", code, SYSTEM_DB_PATH], {
    encoding: "utf8",
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  return JSON.parse(raw || "{}");
}

function assertGeneratedMessage(row) {
  if (!row || !row.id) {
    throw new Error("missed_dose_chat_message_not_found");
  }
  const metadata = row.metadata || {};
  const llm = metadata.llm_personalization || {};
  const validation = llm.llm_message_validation || {};
  const tonePolicy = metadata.tone_policy || {};
  const message = String(row.content || "").trim();
  const candidate = String(llm.llm_message_candidate || "").trim();

  if (llm.message_source !== "llm_generated") {
    throw new Error(`message_source_not_llm_generated:${JSON.stringify(llm)}`);
  }
  if (!candidate || candidate !== message) {
    throw new Error(`candidate_not_displayed:${JSON.stringify({ message, candidate })}`);
  }
  if (validation.passed !== true) {
    throw new Error(`llm_message_validation_failed:${JSON.stringify(validation)}`);
  }
  if (tonePolicy.tone_key !== llm.llm_tone_key) {
    throw new Error(`tone_mismatch:${JSON.stringify({ tonePolicy, llmTone: llm.llm_tone_key })}`);
  }
  if (message.length > 45) {
    throw new Error(`generated_message_too_long:${message}`);
  }
  for (const forbidden of ["당뇨약", "혈압약", "고지혈증약", "타목시펜", "레트로졸", "암", "병기", "재발 위험", "치료 실패", "생명", "반드시", "복용하세요"]) {
    if (message.includes(forbidden)) {
      throw new Error(`generated_message_contains_forbidden_term:${forbidden}:${message}`);
    }
  }
}

async function main() {
  await postForm("/simulation/reset");
  const feed = await getJson("/api/notifications/feed?after_id=0");
  const currentDate = feed.current_time.slice(0, 10);
  await postForm("/medications", {
    medication_choice: "당뇨약",
    dosage_choice: "1정",
    start_date: currentDate,
    end_date: currentDate,
    schedule_template: "custom",
    times_csv: "08:00",
    instructions: `real LLM generated missed-dose message ${RUN_ID}`,
  });
  await postForm("/phr/register");
  await postForm("/clock/advance", { minutes: "180" });

  const row = await waitFor("llm_generated_missed_dose_chat", () => {
    const latest = queryLatestMissedDoseChat();
    assertGeneratedMessage(latest);
    return latest;
  });

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  try {
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.getByText(row.content, { exact: false }).first().waitFor({ timeout: 30000 });
    await page.getByRole("button", { name: "Chat" }).click();
    await page.waitForTimeout(500);
    await page.screenshot({ path: path.join(OUT_DIR, "generated-message-visible.png"), fullPage: true });
  } catch (error) {
    const body = await page.locator("body").innerText().catch(() => "");
    fs.writeFileSync(
      path.join(OUT_DIR, "ui-debug.json"),
      JSON.stringify({ error: error.message || String(error), expectedMessage: row.content, body: body.slice(0, 4000) }, null, 2),
      "utf8",
    );
    await page.screenshot({ path: path.join(OUT_DIR, "ui-failed.png"), fullPage: true }).catch(() => {});
    throw error;
  }
  await browser.close();

  const summary = {
    status: "passed",
    runId: RUN_ID,
    baseUrl: BASE_URL,
    outputDir: OUT_DIR,
    systemDbPath: SYSTEM_DB_PATH,
    message: row.content,
    metadata: {
      adherence_pattern: row.metadata.adherence_pattern,
      tone_policy: row.metadata.tone_policy,
      llm_personalization: row.metadata.llm_personalization,
    },
  };
  fs.writeFileSync(path.join(OUT_DIR, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
  console.log(JSON.stringify(summary, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

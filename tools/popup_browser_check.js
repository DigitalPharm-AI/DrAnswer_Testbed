const { chromium } = require("playwright-core");

async function getCurrentSimTime() {
  const response = await fetch("http://127.0.0.1:8000/api/notifications/feed?after_id=0");
  if (!response.ok) {
    throw new Error(`failed_to_read_clock:${response.status}`);
  }
  const payload = await response.json();
  return payload.current_time;
}

async function postForm(url, values) {
  const body = new URLSearchParams(values);
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!response.ok && response.status !== 204) {
    throw new Error(`post_failed:${url}:${response.status}`);
  }
}

async function main() {
  const currentTime = await getCurrentSimTime();
  const currentDate = currentTime.slice(0, 10);
  const currentClockTime = currentTime.slice(11, 16);
  const medicationName = `팝업검증약-${Date.now()}`;
  const scheduleTemplate = currentClockTime <= "08:00" ? "morning_evening" : "morning_lunch_evening";
  const advanceMinutes =
    currentClockTime <= "08:00"
      ? "1"
      : currentClockTime <= "13:00"
        ? "1"
        : currentClockTime <= "21:00"
          ? String(Math.max(1, (21 - Number(currentClockTime.slice(0, 2))) * 60))
          : "720";

  await postForm("http://127.0.0.1:8000/medications", {
    medication_choice: "__custom__",
    medication_name_custom: medicationName,
    dosage_choice: "1정",
    start_date: currentDate,
    end_date: currentDate,
    schedule_template: scheduleTemplate,
    instructions: "팝업 브라우저 검증",
  });

  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });

  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
  await page.goto("http://127.0.0.1:8000/?page=dashboard", { waitUntil: "domcontentloaded", timeout: 15000 });
  await page.waitForTimeout(1000);

  await postForm("http://127.0.0.1:8000/clock/advance", { minutes: advanceMinutes });

  await page.waitForSelector(".popup-toast", { timeout: 10000 });
  await page.screenshot({ path: "artifacts/popup-dashboard.png", fullPage: false });
  console.log(`popup-captured:${medicationName}`);

  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

const { chromium } = require("playwright-core");

async function main() {
  const browser = await chromium.launch({
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless: true,
  });

  for (const pageName of ["dashboard", "alerts", "assistant"]) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
    await page.goto(`http://127.0.0.1:8000/?page=${pageName}`, { waitUntil: "domcontentloaded", timeout: 15000 });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `artifacts/${pageName}.png`, fullPage: true });
    console.log(`captured:${pageName}`);
    await page.close();
  }

  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

// Run in the disposable browser bench image, against an explicit internal UI URL.
const { chromium } = require("playwright");
const { expect } = require("@playwright/test");

async function main() {
  const [base, ...cameras] = process.argv.slice(2);
  if (!base || !cameras.length) {
    throw new Error("Usage: node live_browser_uat.cjs URL CAMERA [CAMERA...]");
  }
  const browser = await chromium.launch({ headless: true });
  try {
    for (const mobile of [false, true]) {
      const context = await browser.newContext({
        viewport: mobile ? { width: 390, height: 844 } : { width: 1920, height: 1080 },
        isMobile: mobile,
        hasTouch: mobile,
        userAgent: mobile
          ? "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
          : undefined,
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      for (const camera of cameras) {
        await page.goto(`${base}/#${encodeURIComponent(camera)}`);
        const toggle = page.getByRole("button", { name: "Toggle live stream quality" });
        await toggle.waitFor({ timeout: 30000 });
        await expect(toggle).toHaveText("SD");
        const metadata = page.waitForResponse(
          (response) => response.url().endsWith(`/api/go2rtc/streams/${camera}_main`) && response.status() === 200,
          { timeout: 20000 },
        );
        await toggle.click();
        const stream = await (await metadata).json();
        if (!stream.producers?.length) throw new Error(`${camera}: main stream has no producer`);
        await expect(toggle).toHaveText("HD");
        await toggle.click();
        await expect(toggle).toHaveText("SD");
        const now = Date.now() / 1000;
        const availability = await (await context.request.get(
          `${base}/api/${camera}/recordings/main_availability?after=${now - 3600}&before=${now - 60}`,
        )).json();
        const latest = availability.ranges?.at(-1);
        if (!latest) throw new Error(`${camera}: no recent HD range for History UAT`);
        const timestamp = Math.floor((latest.start_time + latest.end_time) / 2);
        await page.goto(`${base}/review?timestamp=${camera}_${timestamp}`);
        const history = page.getByRole("button", { name: "Toggle stream quality", exact: true });
        await history.waitFor({ timeout: 30000 });
        await expect(history).toHaveText("SD");
        await history.click();
        await expect(history).toHaveText("HD");
        await history.click();
        console.log(JSON.stringify({ camera, mobile, live: "SD-HD-SD", history: "SD-HD-SD", mainProducers: stream.producers.length }));
      }
      await context.close();
      if (errors.length) throw new Error(`Browser errors: ${errors.join("; ")}`);
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});

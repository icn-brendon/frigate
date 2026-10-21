import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import { configFactory } from "../fixtures/mock-data/config";

async function configureDualStream(app: FrigateApp) {
  const config = configFactory();
  config.cameras.front_door.ffmpeg.inputs.push({
    path: "rtsp://127.0.0.1:8554/front_door_main",
    roles: ["record_events"],
  });
  await app.page.route("**/api/config", (route) =>
    route.fulfill({ json: config }),
  );
  await app.page.route("**/api/go2rtc/streams/front_door_main", (route) =>
    route.fulfill({ json: { producers: [], consumers: [] } }),
  );
}

test.describe("Dual-stream release bench @critical", () => {
  test("live view selects HD and switches back to SD", async ({ frigateApp }) => {
    await configureDualStream(frigateApp);
    await frigateApp.goto("/#front_door");
    const toggle = frigateApp.page.getByRole("button", {
      name: "Toggle live stream quality",
    });
    await expect(toggle).toHaveText("SD");
    const mainStreamRequest = frigateApp.page.waitForRequest(
      "**/api/go2rtc/streams/front_door_main",
    );
    await toggle.click();
    await mainStreamRequest;
    await expect(toggle).toHaveText("HD");
    await toggle.click();
    await expect(toggle).toHaveText("SD");
  });

  test("legacy single-stream camera has no live quality toggle", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/#front_door");
    await expect(
      frigateApp.page.getByRole("button", { name: "Toggle live stream quality" }),
    ).toHaveCount(0);
  });

  test("History switches between HD and SD", async ({ frigateApp }) => {
    await configureDualStream(frigateApp);
    const timestamp = Math.floor(Date.now() / 1000) - 300;
    await frigateApp.page.route("**/api/*/recordings**", (route) =>
      route.fulfill({ json: [] }),
    );
    await frigateApp.page.route("**/api/recordings/unavailable**", (route) =>
      route.fulfill({ json: [] }),
    );
    await frigateApp.page.route("**/api/*/recordings/main_availability**", (route) =>
      route.fulfill({
        json: {
          ranges: [{ start_time: timestamp - 60, end_time: timestamp + 60 }],
          truncated: false,
        },
      }),
    );
    await frigateApp.goto(`/review?timestamp=front_door_${timestamp}`);
    const toggle = frigateApp.page.getByRole("button", {
      name: "Toggle stream quality",
      exact: true,
    });
    await expect(toggle).toHaveText("SD");
    await toggle.click();
    await expect(toggle).toHaveText("HD");
    await toggle.click();
    await expect(toggle).toHaveText("SD");
  });
});

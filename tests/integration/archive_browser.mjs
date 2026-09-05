import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(new URL("./browser/package.json", import.meta.url));
const { chromium } = require("playwright");

const baseURL = process.env.ZONT_ARCHIVE_E2E_URL ?? "http://127.0.0.1:18086";
const credentials = { username: "stage18", password: "stage18-secret" };

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ httpCredentials: credentials, viewport: { width: 1280, height: 900 } });
const page = await context.newPage();

try {
  const anonymous = await browser.newContext();
  for (const path of ["/", "/latest.html", "/reports.json", "/daily/2026-08-05.html", "/api/health", "/za/"] ) {
    const unauthenticated = await anonymous.request.get(`${baseURL}${path}`);
    assert.equal(unauthenticated.status(), 401, `${path} must remain behind Basic Auth`);
  }
  await anonymous.close();

  for (const path of ["/", "/latest.html", "/daily/2026-08-05.html", "/za/latest.html", "/za/daily/2026-08-05.html"]) {
    const response = await context.request.get(`${baseURL}${path}`);
    assert.equal(response.status(), 200, `${path} is a protected direct report URL`);
    assert.match(await response.text(), /archive-nojs[\s\S]*latest\.html/, `${path} retains a no-JS latest fallback`);
  }

  await page.goto(`${baseURL}/`, { waitUntil: "networkidle" });
  assert.match(page.url(), /\/$/, "root keeps its stable landing URL");
  await page.locator("[data-archive-navigation]").waitFor();
  assert.equal(await page.locator('[data-archive-action="latest"][href]').count(), 0);

  const controls = page.locator(".archive-controls");
  await assert.equal(await controls.isVisible(), true, "controls appear after the manifest loads");
  assert.equal(await page.locator(".archive-day.available").count(), 3, "only completed sparse daily reports are selectable");
  assert.equal(await page.locator(".archive-day.available.selected").count(), 1);

  await page.locator('[data-archive-action="previous"]').click();
  await page.waitForURL("**/daily/2026-08-03.html");
  await page.locator('[data-archive-action="previous"]').click();
  await page.waitForURL("**/daily/2026-08-01.html");
  await page.locator('[data-archive-action="next"]').click();
  await page.waitForURL("**/daily/2026-08-03.html");
  await page.locator('[data-archive-action="latest"]').click();
  await page.waitForURL("**/daily/2026-08-05.html");

  const beforeMonthArrow = page.url();
  await page.locator('[data-archive-month="previous"]').click();
  assert.equal(page.url(), beforeMonthArrow, "month arrows change the calendar only");
  await page.locator('[data-archive-month="next"]').click();
  assert.equal(page.url(), beforeMonthArrow, "month arrows never navigate the report");

  await page.locator('[data-archive-kind="weekly"]').click();
  const weekly = page.locator(".archive-periods a");
  await assert.equal(await weekly.count(), 1);
  await assert.match(await weekly.first().textContent(), /27 июля 2026.*3 августа 2026/s);
  await weekly.first().click();
  await page.waitForURL("**/weekly/2026-07-27.html");

  await page.locator('[data-archive-kind="monthly"]').click();
  const monthly = page.locator(".archive-periods a");
  await assert.equal(await monthly.count(), 1);
  await assert.match(await monthly.first().textContent(), /1 июля 2026.*1 августа 2026/s);
  await monthly.first().click();
  await page.waitForURL("**/monthly/2026-07-01.html");

  await page.goto(`${baseURL}/daily/2026-08-05.html`, { waitUntil: "networkidle" });
  const card = page.locator(".recommendation[data-recommendation-id]").first();
  await card.waitFor();
  const note = "Stage 1.8 browser feedback survives reload";
  await card.locator(".feedback-note").fill(note);
  await card.locator('button[data-feedback-status="applied"]').click();
  await card.locator(".feedback-message").filter({ hasText: "сохранена" }).waitFor();
  await assert.match(await card.locator(".feedback-message").textContent(), /сохранена/i);
  await page.reload({ waitUntil: "networkidle" });
  await assert.equal(await page.locator(".recommendation .feedback-note").first().inputValue(), note);
  await assert.match(await page.locator(".recommendation .feedback-status").first().textContent(), /Выполнено/);

  const legacy = await context.request.get(`${baseURL}/za/daily/2026-08-05.html`);
  assert.equal(legacy.status(), 200, "legacy /za archive remains reachable");
  const rootHealth = await context.request.get(`${baseURL}/api/health`);
  const legacyHealth = await context.request.get(`${baseURL}/za/api/health`);
  assert.equal(rootHealth.status(), 200);
  assert.equal(legacyHealth.status(), 200);

  await page.goto(`${baseURL}/za/daily/2026-08-05.html`, { waitUntil: "networkidle" });
  await page.locator('[data-archive-action="previous"]').click();
  await page.waitForURL("**/za/daily/2026-08-03.html");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "/tmp/zont-stage18-archive-mobile.png", fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
} finally {
  await browser.close();
}

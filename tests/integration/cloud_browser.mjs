import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(new URL("./browser/package.json", import.meta.url));
const { chromium } = require("playwright");
const base = process.env.ZONT_CLOUD_BROWSER_URL ?? "http://127.0.0.1:18087";
const credentials = { username: "browser", password: "fixture-secret" };
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ httpCredentials: credentials, viewport: { width: 1280, height: 900 } });
const page = await context.newPage();

async function drain() {
  for (let attempt = 0; attempt < 6; attempt++) {
    const response = await context.request.post(`${base}/jobs/publication`, { data: {} });
    assert.equal(response.status(), 200, `publication job: ${await response.text()}`);
    if ((await response.json()).result.pending_reports === 0) return;
  }
  assert.fail("publication queue did not drain after six bounded jobs");
}

try {
  const anonymous = await browser.newContext();
  for (const path of ["/", "/latest.html", "/reports.json", "/daily/2026-08-05.html",
    "/api/health", "/za/api/health", "/za/latest.html"]) {
    const response = await anonymous.request.get(`${base}${path}`);
    assert.equal(response.status(), 401, `${path} must require Basic Auth`);
  }
  await anonymous.close();

  const manifestResponse = await context.request.get(`${base}/reports.json`);
  assert.equal(manifestResponse.status(), 200);
  const manifest = await manifestResponse.json();
  assert.deepEqual(new Set(manifest.reports.map(item => item.kind)),
    new Set(["daily", "weekly", "monthly", "seasonal"]));
  for (const entry of manifest.reports) {
    assert.match(entry.href, /^(daily|weekly|monthly|seasonal)\/\d{4}-\d{2}-\d{2}\.html$/);
    for (const path of [entry.href, entry.href.replace(/\.html$/, ".json")]) {
      const response = await context.request.get(`${base}/${path}`);
      assert.equal(response.status(), 200, path);
    }
  }
  assert.equal((await context.request.get(`${base}/za/reports.json`)).status(), 200);
  assert.equal((await context.request.get(`${base}/za/latest.html`)).status(), 200);
  assert.equal((await context.request.get(`${base}/api/health`)).status(), 200);
  assert.equal((await context.request.get(`${base}/za/api/health`)).status(), 200);

  const latestResponse = await context.request.get(`${base}/latest.html`);
  const latest = await latestResponse.text();
  assert.equal(latestResponse.status(), 200);
  assert.match(latest, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.doesNotMatch(latest, /<script>unsafe<\/script>/);
  await page.goto(`${base}/latest.html`, { waitUntil: "networkidle" });
  await page.locator("[data-archive-navigation]").waitFor();
  assert.equal(await page.locator("[data-owner-forms]").getAttribute("data-device-id"), "browser-synthetic-device");

  const hrefs = Object.fromEntries(manifest.reports.map(item => [item.kind, item.href]));
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(`${base}/${hrefs.daily}`, { waitUntil: "networkidle" });
    for (const kind of ["weekly", "monthly", "seasonal", "daily"]) {
      await page.locator(`[data-archive-kind="${kind}"]`).click();
      await page.waitForURL(`${base}/${hrefs[kind]}`, { waitUntil: "networkidle" });
      assert.equal(await page.locator(`[data-archive-kind="${kind}"]`).getAttribute("aria-selected"), "true");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
        `${kind} must fit ${width}px viewport`);
    }
  }

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`${base}/latest.html`, { waitUntil: "networkidle" });
  await page.locator("#system-profile > summary").click();
  const notes = page.locator('[data-field="installation_notes"] input');
  await notes.fill("Cloud owner <safe>");
  await page.locator("[data-profile-save]").click();
  await page.locator("[data-profile-message]").filter({ hasText: "сохранён" }).waitFor();
  await drain();
  await page.reload({ waitUntil: "networkidle" });
  assert.equal(await notes.inputValue(), "Cloud owner <safe>");
  assert.equal(await page.locator('[data-field="installation_notes"] safe').count(), 0);

  const reportId = await page.locator("[data-owner-forms]").getAttribute("data-report-id");
  const gas = page.locator("[data-owner-gas]");
  await gas.locator("[data-gas-edit]").click();
  await gas.locator("[name=gas-date]").fill("2026-08-05");
  await gas.locator("[name=gas-value]").fill("10");
  await gas.locator("[data-gas-save]").click();
  await gas.locator("[data-gas-message]").filter({ hasText: "сохранено" }).waitFor();
  assert.equal((await (await context.request.get(`${base}/api/reports/${encodeURIComponent(reportId)}/gas`)).json())
    .reading.value_m3, "10");
  await drain();
  await page.reload({ waitUntil: "networkidle" });
  assert.match(await page.locator("[data-owner-gas]").textContent(), /10/);

  await page.locator("[data-tariff-edit]").click();
  const tariffEditor = page.locator('[data-owner-gas] #tariff-editor');
  const plannedMonth = await tariffEditor.locator("[data-tariff-month]").inputValue();
  await tariffEditor.locator("[data-tariff-price]").fill("9");
  await tariffEditor.locator("[data-tariff-save]").click();
  await tariffEditor.locator("[data-tariff-message]").filter({ hasText: "Тариф сохранён" }).waitFor();
  await drain();
  const tariffs = (await (await context.request.get(`${base}/api/gas-tariffs`)).json()).history;
  assert.equal(tariffs.find(item => item.effective_month === plannedMonth).price, "9");
  await page.reload({ waitUntil: "networkidle" });
  assert.match(await page.locator("[data-tariff-planned]").textContent(), /9 RUB/);

  const card = page.locator(".recommendation[data-recommendation-id]").first();
  await card.waitFor();
  await card.locator(".feedback-comment > summary").click();
  await card.locator(".feedback-note").fill("Cloud feedback persists");
  await card.locator(".feedback-experiment > summary").click();
  await card.locator('[data-experiment-field="category"]').selectOption("firmware_update");
  await card.locator('[data-experiment-field="parameter"]').fill("Synthetic firmware");
  await card.locator('[data-experiment-field="before"]').fill("1.0");
  await card.locator('[data-experiment-field="after"]').fill("1.1");
  await card.locator('[data-experiment-field="performed_at"]').fill("2026-08-05T12:30");
  await card.locator('button[data-feedback-status="applied"]').click();
  await card.locator(".feedback-message").filter({ hasText: "сохранена" }).waitFor();
  await drain();
  await page.reload({ waitUntil: "networkidle" });
  assert.equal(await page.locator(".recommendation .feedback-note").first().inputValue(), "Cloud feedback persists");
  assert.equal(await page.locator('[data-experiment-field="category"]').first().inputValue(), "firmware_update");

  await page.locator("#ai-settings > summary").click();
  const interval = page.locator("[data-ai-review-interval]");
  await interval.waitFor({ state: "visible" });
  await interval.fill("61");
  await page.locator("[data-ai-save]").click();
  await page.locator("[data-ai-message]").filter({ hasText: "сохранены" }).waitFor();
  await drain();
  await page.reload({ waitUntil: "networkidle" });
  await page.locator("#ai-settings > summary").click();
  await page.waitForFunction(() => document.querySelector("[data-ai-review-interval]").value === "61");
  assert.equal(await interval.inputValue(), "61");

  console.log("Cloud browser acceptance: auth, four archives, mobile, owner forms, AI settings, and publication passed");
} finally {
  await browser.close();
}

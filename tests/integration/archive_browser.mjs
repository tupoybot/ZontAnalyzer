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

  assert.equal(await page.locator(".archive-picker").getAttribute("open"), null);
  assert.equal(await page.locator("#system-profile").getAttribute("open"), null);
  assert.equal(await page.locator(".feedback-note").first().isVisible(), false);
  assert.equal(await page.locator("#debug-toggle").isChecked(), false);
  await page.locator("#debug-toggle").check();
  assert.equal(await page.locator("body").evaluate(e => e.classList.contains("debug-mode")), true);
  await page.reload({waitUntil: "networkidle"});
  assert.equal(await page.locator("#debug-toggle").isChecked(), true);
  await page.locator("#debug-toggle").uncheck();
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

  await page.locator(".archive-picker > summary").click();
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
  assert.match(await page.locator(".reasoning-item.hypothesis").textContent(), /Синтетическая гипотеза <unsafe>/);
  assert.match(await page.locator(".reasoning-item.hypothesis").textContent(), /неподтверждённая ссылка/);
  assert.equal(await page.locator(".reasoning-item unsafe").count(), 0);
  const ownerForm = page.locator("[data-owner-forms]");
  await ownerForm.waitFor();
  const dayFiveReportId = await ownerForm.getAttribute("data-report-id");
  assert.equal(await ownerForm.getAttribute("data-device-id"), "browser-synthetic-device");
  assert.equal(await ownerForm.locator("[data-field=installation_notes] input").inputValue(), "<b>synthetic owner note</b>");
  assert.equal(await ownerForm.locator("[data-field=installation_notes] b").count(), 0, "owner text is not interpreted as HTML");
  await ownerForm.locator(".owner-equipment > summary").click();
  assert.equal(await ownerForm.locator('.owner-tristate').count(), 0);
  const sizes = await ownerForm.locator('.owner-reset, .owner-field input').evaluateAll(nodes =>
    nodes.filter(n => n.getBoundingClientRect().height > 0).map(n => n.getBoundingClientRect().height));
  assert.ok(sizes.every(h => h <= 60), 'profile controls keep their natural height');
  const editGas = ownerForm.locator('[data-gas-edit]');
  await editGas.hover();
  const hoverColors = await editGas.evaluate(n => ({color:getComputedStyle(n).color, background:getComputedStyle(n).backgroundColor}));
  assert.equal(hoverColors.color, 'rgb(255, 255, 255)');
  assert.equal(hoverColors.background, 'rgb(57, 75, 96)');
  assert.equal(await page.locator('.kpi-grid .kpi-uptime-row .kpi').count(), 2);
  const profileRequests = [];
  page.on("request", request => {
    if (request.method() === "PUT" && request.url().includes("/equipment/")) profileRequests.push(request.postDataJSON());
  });
  const autoAdapt = ownerForm.locator("[data-field=auto_adapt]");
  await autoAdapt.locator(".owner-unknown").selectOption("yes");
  await ownerForm.locator("[data-profile-save]").click();
  await ownerForm.locator("[data-profile-message]").filter({ hasText: "сохранён" }).waitFor();
  assert.deepEqual(Object.keys(profileRequests.at(-1).fields), ["auto_adapt", "auto_adapt_node"], "first save includes the default node");
  assert.equal(profileRequests.at(-1).fields.auto_adapt_node.value, "Рециркуляция ГВС");
  assert.equal(await ownerForm.locator("[data-field=auto_adapt_node] select").inputValue(), "Рециркуляция ГВС");
  for (const removed of ["gas_unit", "gas_source", "gas_applicability"]) {
    assert.equal(await ownerForm.locator(`[data-field=${removed}]`).count(), 0);
  }
  assert.equal(profileRequests.at(-1).fields.auto_adapt.value, true);
  await autoAdapt.locator(".owner-unknown").selectOption("no");
  await ownerForm.locator("[data-profile-save]").click();
  await ownerForm.locator("[data-profile-message]").filter({ hasText: "сохранён" }).waitFor();
  assert.equal(profileRequests.at(-1).fields.auto_adapt.value, false);
  assert.deepEqual(Object.keys(profileRequests.at(-1).fields), ["auto_adapt"], "saved node is not overwritten on unrelated changes");
  await autoAdapt.locator(".owner-reset").click();
  await ownerForm.locator("[data-profile-message]").filter({ hasText: "сброшено" }).waitFor();
  assert.equal((await context.request.get(`${baseURL}/api/equipment`)).status(), 200);

  const gasType = ownerForm.locator("[data-field=gas_type] select.owner-value");
  assert.equal(await gasType.inputValue(), "", "unknown gas is not silently defaulted");
  await gasType.selectOption("Природный газ (метан)");
  await ownerForm.locator("[data-profile-save]").click();
  await ownerForm.locator("[data-profile-message]").filter({ hasText: "сохранён" }).waitFor();
  assert.equal(profileRequests.at(-1).fields.gas_type.value, "Природный газ (метан)");
  await page.reload({ waitUntil: "networkidle" });
  assert.equal(await gasType.inputValue(), "Природный газ (метан)");
  await context.request.put(`${baseURL}/api/equipment/browser-synthetic-device`, {
    data: {fields: {gas_type: {value: "Исторический газ <custom>"}}},
  });
  await page.reload({ waitUntil: "networkidle" });
  assert.equal(await gasType.inputValue(), "Исторический газ <custom>", "legacy gas survives refresh");
  assert.equal(await gasType.locator("custom").count(), 0);

  const gas = ownerForm.locator("[data-owner-gas]");
  await gas.locator("[data-gas-edit]").click();
  await gas.locator("[name=gas-value]").fill("10");
  await gas.locator("[data-gas-save]").click();
  await gas.locator("[data-gas-message]").filter({ hasText: "сохранено" }).waitFor();
  const dayFiveGas = await context.request.get(`${baseURL}/api/reports/${encodeURIComponent(dayFiveReportId)}/gas`);
  assert.equal(dayFiveGas.status(), 200);
  assert.equal((await dayFiveGas.json()).reading.value_m3, "10");
  await page.goto(`${baseURL}/daily/2026-08-01.html`, { waitUntil: "networkidle" });
  const historicalGas = page.locator("[data-owner-gas]");
  const historicalReportId = await page.locator("[data-owner-forms]").getAttribute("data-report-id");
  await historicalGas.locator("[data-gas-edit]").click();
  await historicalGas.locator("[name=gas-value]").fill("5");
  await historicalGas.locator("[data-gas-save]").click();
  await historicalGas.locator("[data-gas-message]").filter({ hasText: "сохранено" }).waitFor();
  assert.equal((await (await context.request.get(`${baseURL}/api/reports/${encodeURIComponent(historicalReportId)}/gas`)).json()).reading.value_m3, "5");
  assert.equal((await (await context.request.get(`${baseURL}/api/reports/${encodeURIComponent(dayFiveReportId)}/gas`)).json()).reading.value_m3, "10", "different report days keep separate readings");
  await historicalGas.locator("[data-gas-edit]").click();
  await historicalGas.locator("[name=gas-value]").fill("12");
  await historicalGas.locator("[data-gas-save]").click();
  await historicalGas.locator("[data-gas-message]").filter({ hasText: /конфликт|conflict/i }).waitFor();
  await historicalGas.locator("[name=gas-reset]").check();
  await historicalGas.locator("[name=gas-value]").fill("0");
  await historicalGas.locator("[data-gas-save]").click();
  await historicalGas.locator("[data-gas-message]").filter({ hasText: "сохранено" }).waitFor();
  const resetGas = await (await context.request.get(`${baseURL}/api/reports/${encodeURIComponent(historicalReportId)}/gas`)).json();
  assert.match(resetGas.reading.meter_segment, /^reset:/, "reset creates a meter boundary");
  await historicalGas.locator("[data-gas-edit]").click();
  await historicalGas.locator("[data-gas-delete]").click();
  await historicalGas.locator("[data-gas-message]").filter({ hasText: "удалено" }).waitFor();
  assert.equal((await (await context.request.get(`${baseURL}/api/reports/${encodeURIComponent(historicalReportId)}/gas`)).json()).reading, null);
  await page.goto(`${baseURL}/latest.html`, { waitUntil: "networkidle" });
  assert.equal(await page.locator("[data-owner-forms][data-report-id]").count(), 1);
  assert.equal(await page.locator("[data-owner-gas] [name=gas-value]").inputValue(), "10", "latest uses the displayed report's reading");
  const card = page.locator(".recommendation[data-recommendation-id]").first();
  await card.waitFor();
  const note = "Stage 1.8 browser feedback survives reload";
  await card.locator(".feedback-comment > summary").click();
  await card.locator(".feedback-note").fill(note);
  await card.locator('button[data-feedback-status="applied"]').click();
  await card.locator(".feedback-message").filter({ hasText: "сохранена" }).waitFor();
  await assert.match(await card.locator(".feedback-message").textContent(), /сохранена/i);
  await page.reload({ waitUntil: "networkidle" });
  await assert.equal(await page.locator(".recommendation .feedback-note").first().inputValue(), note);
  await assert.match(await page.locator(".recommendation .feedback-status").first().textContent(), /Выполнено/);

  await page.locator(".recommendation .feedback-comment > summary").first().click();
  await page.locator(".recommendation .feedback-note").first().fill(note + " edited");
  await page.locator(".recommendation [data-feedback-save-note]").first().click();
  await page.locator(".recommendation .feedback-message").first().filter({hasText:"сохранена"}).waitFor();
  await page.reload({waitUntil: "networkidle"});
  assert.equal(await page.locator(".recommendation .feedback-note").first().inputValue(), note + " edited");
  assert.match(await page.locator(".recommendation .feedback-status").first().textContent(), /Выполнено/);
  await page.goto(`${baseURL}/latest.html?debug=1`, {waitUntil: "networkidle"});
  assert.equal(await page.locator("#debug-toggle").isChecked(), true);
  await page.locator("#debug-toggle").uncheck();
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

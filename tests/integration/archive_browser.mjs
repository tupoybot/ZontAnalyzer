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

  // Changing report kind opens its newest publication with the picker closed.
  for (const prefix of ["/", "/za/"]) {
    await page.setViewportSize({width: prefix === "/" ? 1280 : 390, height: 900});
    await page.goto(`${baseURL}${prefix}daily/2026-08-01.html`, {waitUntil: "networkidle"});
    for (const [kind, date] of [["weekly", "2026-07-27"], ["monthly", "2026-07-01"],
      ["seasonal", "2026-09-01"], ["daily", "2026-08-05"]]) {
      await page.locator('[data-archive-kind="' + kind + '"]').click();
      await page.waitForURL(`${baseURL}${prefix}${kind}/${date}.html`, {waitUntil: "networkidle"});
      assert.equal(await page.locator(".archive-picker").getAttribute("open"), null);
      assert.equal(await page.locator('[data-archive-kind="' + kind + '"]').getAttribute("aria-selected"), "true");
      await page.locator(".archive-picker > summary").click();
      assert.equal(await page.locator(kind === "daily" ? ".archive-day.available" : ".archive-periods a").count(), 3);
    }
  }

  // Long-period arrows must follow published neighbours, skipping missing periods.
  for (const prefix of ["/", "/za/"]) {
    await page.setViewportSize({width: prefix === "/" ? 1280 : 390, height: 900});
    for (const [kind, dates] of [
      ["weekly", ["2026-06-29", "2026-07-13", "2026-07-27"]],
      ["monthly", ["2026-04-01", "2026-06-01", "2026-07-01"]],
      ["seasonal", ["2025-09-01", "2026-03-01", "2026-09-01"]],
    ]) {
      const before = page.locator('[data-archive-action="previous"]');
      const after = page.locator('[data-archive-action="next"]');
      const newest = page.locator('[data-archive-action="latest"]');
      const url = date => `${baseURL}${prefix}${kind}/${date}.html`;
      await page.goto(url(dates[2]), {waitUntil: "networkidle"});
      assert.equal(await after.isDisabled(), true, `${kind}: newest has no next report`);
      assert.equal(await newest.textContent(), "Последний");
      for (const index of [1, 0]) {
        await before.click();
        await page.waitForURL(url(dates[index]), {waitUntil: "networkidle"});
      }
      assert.equal(await before.isDisabled(), true, `${kind}: oldest has no previous report`);
      for (const index of [1, 2]) {
        await after.click();
        await page.waitForURL(url(dates[index]), {waitUntil: "networkidle"});
      }
      await before.click();
      await page.waitForURL(url(dates[1]), {waitUntil: "networkidle"});
      await newest.click();
      await page.waitForURL(url(dates[2]), {waitUntil: "networkidle"});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
  }
  await page.setViewportSize({width: 1280, height: 900});
  await page.route("**/reports.json", async route => {
    const response = await route.fetch();
    const manifest = await response.json();
    manifest.reports = manifest.reports.filter(item => item.kind !== "seasonal");
    await route.fulfill({response, json: manifest});
  });
  await page.goto(`${baseURL}/weekly/2026-07-27.html`, {waitUntil: "networkidle"});
  await page.locator('[data-archive-kind="seasonal"]').click();
  assert.equal(page.url(), `${baseURL}/weekly/2026-07-27.html`, "empty kind keeps current report");
  assert.equal(await page.locator('[data-archive-kind="weekly"]').getAttribute("aria-selected"), "true");
  assert.equal(await page.locator(".archive-picker").getAttribute("open"), null);
  assert.match(await page.locator(".archive-status").textContent(), /Нет опубликованных отчётов/);
  await page.unroute("**/reports.json");

  await page.goto(`${baseURL}/daily/2026-08-05.html`, { waitUntil: "networkidle" });
  const gasCard = page.locator(".gas-period-card");
  await gasCard.waitFor();
  assert.match(await gasCard.textContent(), /Расход газа за период/);
  assert.match(await gasCard.textContent(), /12,3 м³/);
  assert.match(await gasCard.textContent(), /Индекс надёжности/);
  assert.match(await gasCard.textContent(), /Модель: gas-browser-1/);
  assert.match(await gasCard.textContent(), /Объяснение AI устарело/);
  for (const [kind, date, marker] of [["weekly", "2026-07-27", "измерено"],
    ["monthly", "2026-07-01", "экстраполировано"], ["seasonal", "2026-09-01", "оценено"]]) {
    await page.goto(`${baseURL}/${kind}/${date}.html`, {waitUntil: "networkidle"});
    const periodGas = page.locator(".gas-period-card");
    await periodGas.waitFor();
    assert.match(await periodGas.textContent(), new RegExp(marker));
    assert.match(await periodGas.textContent(), /Покрытие/);
  }
  await page.goto(`${baseURL}/daily/2026-08-05.html`, { waitUntil: "networkidle" });
  const questionBox = page.locator(".counterfactual-question");
  const buttonBox = page.locator(".regenerate-report");
  const desktopGeometry = await page.evaluate(() => {
    const question = document.querySelector(".counterfactual-question").getBoundingClientRect();
    const button = document.querySelector(".regenerate-report").getBoundingClientRect();
    return {questionWidth: question.width, questionHeight: question.height,
      buttonTop: button.top, questionTop: question.top, buttonLeft: button.left,
      questionRight: question.right};
  });
  assert.ok(desktopGeometry.questionWidth >= 300, "desktop question field remains readable");
  assert.ok(desktopGeometry.questionHeight >= 60, "question field has a usable height");
  assert.ok(desktopGeometry.buttonTop >= desktopGeometry.questionTop, "button aligns with question row");
  assert.ok(desktopGeometry.buttonLeft >= desktopGeometry.questionRight - 1, "button does not overlap field");
  await page.setViewportSize({width: 390, height: 844});
  const mobileGeometry = await page.evaluate(() => {
    const question = document.querySelector(".counterfactual-question").getBoundingClientRect();
    const button = document.querySelector(".regenerate-report").getBoundingClientRect();
    const root = document.querySelector(".report-regeneration").getBoundingClientRect();
    return {questionWidth: question.width, rootWidth: root.width, questionBottom: question.bottom,
      buttonTop: button.top, buttonWidth: button.width};
  });
  assert.ok(mobileGeometry.questionWidth >= mobileGeometry.rootWidth - 2, "mobile question uses full width");
  assert.ok(mobileGeometry.buttonTop >= mobileGeometry.questionBottom, "mobile button follows field");
  assert.ok(mobileGeometry.buttonWidth >= mobileGeometry.rootWidth - 2, "mobile button uses full width");
  await page.setViewportSize({width: 1280, height: 900});
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
  assert.equal(await page.locator('.kpi-grid > .kpi:not(.kpi-gas-strip)').count(), 6);
  assert.equal(await page.locator('.kpi-grid > .gas-kpi.kpi-gas-strip').count(), 1);
  assert.equal(await page.locator('.kpi-grid > .kpi-uptime-row').count(), 1);
  const kpiColumns = await page.locator('.kpi-grid').evaluate(grid => {
    const cells = [...grid.querySelectorAll(':scope > .kpi:not(.kpi-gas-strip)')]
      .map(n => n.getBoundingClientRect());
    return cells.length === 6
      && [0, 1, 2].every(i => Math.abs(cells[i + 3].x - cells[i].x) < 1)
      && [0, 1, 2].every(i => Math.abs(cells[i + 3].width - cells[i].width) < 1);
  });
  assert.equal(kpiColumns, true, 'second KPI row aligns with the temperature cards');
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
  await card.locator(".feedback-experiment > summary").click();
  await card.locator('[data-experiment-field="category"]').selectOption("firmware_update");
  await card.locator('[data-experiment-field="parameter"]').fill("Прошивка контроллера");
  await card.locator('[data-experiment-field="before"]').fill("1.0");
  await card.locator('[data-experiment-field="after"]').fill("1.1");
  await card.locator('[data-experiment-field="performed_at"]').fill("2026-08-01T12:30");
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
  assert.equal(await page.locator('[data-experiment-field="category"]').first().inputValue(), "firmware_update");
  assert.equal(await page.locator('[data-experiment-field="before"]').first().inputValue(), "1.0");
  assert.equal(await page.locator('[data-experiment-field="after"]').first().inputValue(), "1.1");
  assert.match(await page.locator('[data-experiment-field="performed_at"]').first().inputValue(), /^2026-08-01T12:30/);
  assert.match(await page.locator(".recommendation .feedback-status").first().textContent(), /Выполнено/);

  await page.goto(`${baseURL}/latest.html`, { waitUntil: "networkidle" });
  const regenerate = page.locator(".regenerate-report");
  await regenerate.waitFor();
  const counterfactual = page.locator(".counterfactual-question");
  await counterfactual.waitFor();
  await assert.equal(await counterfactual.getAttribute("maxlength"), "500");
  const regenerationReload = page.waitForEvent("load", {timeout: 30000});
  await regenerate.click();
  await assert.equal(await regenerate.isDisabled(), true, "duplicate regeneration click is disabled while active");
  await regenerationReload;
  await page.waitForLoadState("networkidle");
  await page.waitForURL("**/latest.html");
  await page.locator(".regenerate-report").waitFor();
  await page.locator("[data-owner-forms]").waitFor();
  assert.equal(await page.locator("[data-owner-gas] [name=gas-value]").inputValue(), "10", "gas survives regeneration");
  assert.equal(await page.locator(".recommendation .feedback-note").first().inputValue(), note + " edited", "feedback survives regeneration");

  await page.locator("#system-profile > summary").click();
  const seasons = page.locator('[data-field="season_boundaries"]');
  await seasons.waitFor({ state: "visible" });
  const seasonValues = {spring: "02-15", summer: "05-20", autumn: "08-25", winter: "11-30"};
  for (const [key, value] of Object.entries(seasonValues)) await seasons.locator(`[data-season="${key}"]`).fill(value);
  await page.locator("[data-profile-save]").click();
  await page.locator("[data-profile-message]").filter({hasText: "сохранён"}).waitFor();
  await page.reload({waitUntil: "networkidle"});
  for (const [key, value] of Object.entries(seasonValues)) assert.equal(await page.locator(`[data-season="${key}"]`).inputValue(), value);

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

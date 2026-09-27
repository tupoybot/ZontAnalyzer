import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(new URL("./browser/package.json", import.meta.url));
const { chromium } = require("playwright");
const shell = readFileSync(new URL("../../infra/static/site.html", import.meta.url), "utf8");
const digest = "a".repeat(64);
const htmlKey = `publication/reports/${digest}.html`;
const index = {
  version: 1,
  updated_at: "2026-09-27T00:00:00Z",
  manifest_key: `publication/manifests/${digest}.json`,
  latest_key: `publication/latest/${digest}.html`,
  reports: [{kind:"daily", start:"2026-09-26", end:"2026-09-27", href:"daily/2026-09-26.html",
    html_key:htmlKey, json_key:`publication/reports/${"b".repeat(64)}.json`}],
};
const browser = await chromium.launch({headless:true});
try {
  const context = await browser.newContext();
  await context.addCookies([{name:"session", value:"fixture", domain:"example.test", path:"/", secure:true, httpOnly:true}]);
  const page = await context.newPage();
  const objectRequests = [];
  let manifestStatus = 200;
  let currentIndex = index;
  await page.route("https://example.test/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/reports.json") {
      await route.fulfill({status:manifestStatus, contentType:"application/json", body:JSON.stringify(currentIndex)});
    } else if (url.pathname.startsWith("/objects/")) {
      objectRequests.push({path:url.pathname, headers:await route.request().allHeaders()});
      await route.fulfill({status:200, contentType:"text/html; charset=utf-8",
        body:"<!doctype html><title>Fixture report</title><p id=report>Published report body</p><script>window.shellKeptPath=location.pathname</script>"});
    } else if (url.pathname === "/auth/login") {
      await route.fulfill({status:200, contentType:"text/html", body:"<h1>Login</h1>"});
    } else {
      await route.fulfill({status:200, contentType:"text/html; charset=utf-8", body:shell});
    }
  });

  await page.goto("https://example.test/daily/2026-09-26.html");
  await page.locator("#report").waitFor();
  assert.equal(await page.locator("#report").textContent(), "Published report body");
  assert.equal(await page.evaluate(() => window.shellKeptPath), "/daily/2026-09-26.html");
  assert.equal(await page.locator('form[action="/auth/logout"][method="post"] button').textContent(), "Выйти");
  assert.equal(objectRequests.length, 1);
  assert.equal(objectRequests[0].path, `/objects/${htmlKey}`);
  assert.match(objectRequests[0].headers.cookie, /session=fixture/);

  const beforeUnknown = objectRequests.length;
  await page.goto("https://example.test/daily/2099-01-01.html");
  assert.match(await page.locator("#site-status").textContent(), /недоступен/i);
  assert.equal(objectRequests.length, beforeUnknown, "unlisted archive paths never become object keys");

  currentIndex = {...index, reports:[{...index.reports[0], html_key:"../private.html"}]};
  await page.goto("https://example.test/daily/2026-09-26.html");
  assert.match(await page.locator("#site-status").textContent(), /недоступен/i);
  assert.equal(objectRequests.length, beforeUnknown, "untrusted index keys never reach object storage");

  currentIndex = index;
  currentIndex = {...index, latest_key:"", reports:[{
    kind:"weekly", start:"2026-09-21", end:"2026-09-27", href:"weekly/2026-09-21.html",
    html_key:htmlKey, json_key:`publication/reports/${"c".repeat(64)}.json`,
  }]};
  await page.goto("https://example.test/weekly/2026-09-21.html");
  await page.locator("#report").waitFor();
  assert.equal(objectRequests.at(-1).path, `/objects/${htmlKey}`,
    "dated reports remain available when no daily latest report exists");

  currentIndex = index;
  manifestStatus = 401;
  await page.goto("https://example.test/latest.html");
  await page.waitForURL("**/auth/login");
} finally {
  await browser.close();
}

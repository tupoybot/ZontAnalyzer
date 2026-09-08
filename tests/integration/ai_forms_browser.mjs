import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(new URL("./browser/package.json", import.meta.url));
const { chromium } = require("playwright");

const root = new URL("../..", import.meta.url).pathname.replace(/\/$/, "");
const html = process.env.AI_FORMS_HTML_FILE
  ? readFileSync(process.env.AI_FORMS_HTML_FILE, "utf8")
  : execFileSync(process.env.PYTHON_BIN ?? "python3", ["-c", "from zont_analyzer.reports.ai_forms import render_ai_forms; print(render_ai_forms())"], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: `${root}/src` },
    encoding: "utf8",
  });

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.route("http://example.test/**", route => route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: `<!doctype html><meta charset="utf-8"><body data-feedback-api-base="/za/api">${html}</body>` }));
  await page.goto("http://example.test/za/report.html");
  await page.evaluate(() => {
    let reviewPolls = 0;
    window.actions = [];
    window.fetch = async (url, options = {}) => {
      if (url.endsWith("/api/ai/review")) {
        if (options.method !== "PUT") throw new Error("review action must use PUT");
        window.actions.push(JSON.parse(options.body));
        return new Response(JSON.stringify({ status: "accepted" }), { status: 202 });
      }
      if (url.endsWith("/api/ai")) {
        reviewPolls += 1;
        const proposals = [
          {id:"daily", version:3, profile:"daily", current_model:"old", candidate_model:"new", recommendation:{reason:"quality", tradeoffs:{quality:"same", latency:"low", price:{current:{input_per_mtok_usd:1}, candidate:{input_per_mtok_usd:.5}}}}},
          {id:"review", version:4, profile:"review", current_model:"old2", candidate_model:"new2", recommendation:{reason:"cost", tradeoffs:{latency:"same"}}},
        ];
        const review = reviewPolls > 2 ? { status: "no_change", last_success_at: "2026-09-08T00:00:00Z", proposals: [], runs: [{status:"no_change", sources:[{url:"https://developers.openai.com"}]}] } : { running: false, proposals, runs: [] };
        return new Response(JSON.stringify({ version: "v1", effective: { enabled: true, daily_model: "gpt-5.6-luna", review_model: "gpt-5.6-terra", daily_reasoning_effort: "medium", review_reasoning_effort: "low", review_enabled: true, review_interval_days: 60 }, overridden: false, history: [], models: [{ id: "gpt-5.6-luna", efforts: ["medium"] }, { id: "gpt-5.6-terra", efforts: ["low"] }], review }), { status: 200 });
      }
      throw new Error(`unexpected ${url}`);
    };
  });
  await page.locator("#ai-settings > summary").click();
  await page.locator("[data-ai-accept]").click();
  await page.waitForTimeout(1100);
  await page.locator("[data-ai-proposal-select]").selectOption("review");
  await page.locator("[data-ai-accept]").click();
  const actions = await page.evaluate(() => window.actions);
  assert.deepEqual(actions.map(item => [item.proposal_id, item.expected_version]), [["daily", 3], ["review", 4]]);
  await page.locator("[data-ai-review-interval]").fill("61");
  await page.locator("[data-ai-save]").click();
  await page.locator("[data-ai-check]").click();
  await page.waitForTimeout(1200);
  assert.ok(await page.locator("[data-ai-review-status]").textContent());
  assert.equal(await page.locator("[data-ai-daily-model]").inputValue(), "gpt-5.6-luna");
  await page.screenshot({ path: "/home/botkin/artifacts/stage9.2/ui/ai-1280.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "/home/botkin/artifacts/stage9.2/ui/ai-390.png", fullPage: true });
} finally {
  await browser.close();
}

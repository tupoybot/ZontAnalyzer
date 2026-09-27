import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { generateKeyPairSync, sign, createHash } from "node:crypto";

const require = createRequire(new URL("./browser/package.json", import.meta.url));
const { chromium } = require("playwright");
const shell = readFileSync(new URL("../../infra/static/site.html", import.meta.url), "utf8")
  .replace("__ZONT_OIDC_CONFIG__", JSON.stringify({clientId:"public-test-client", issuer:"https://auth.yandex.cloud"}));
const {privateKey, publicKey} = generateKeyPairSync("rsa", {modulusLength:2048});
const jwk = {...publicKey.export({format:"jwk"}), kid:"test-key", alg:"RS256", use:"sig"};
const digest = "a".repeat(64);
const htmlKey = `publication/reports/${digest}.html`;
const jsonKey = `publication/reports/${"b".repeat(64)}.json`;
const index = {version:1, manifest_key:`publication/manifests/${digest}.json`,
  latest_key:`publication/latest/${digest}.html`, reports:[{href:"daily/2026-09-26.html", html_key:htmlKey, json_key:jsonKey}]};
const report = `<!doctype html><title>Fixture report</title><p id=report>Published report body</p>
<button id=api>Save</button><a id=archive href="/za/daily/2026-09-26.html">Archive</a>
<a id=json href="/za/daily/2026-09-26.json">JSON</a>
<script>window.shellKeptPath=location.pathname;
document.querySelector('#api').onclick=async()=>{const r=await fetch('/za/api/v1/settings',{method:'PUT',body:'{}'});document.querySelector('#api').textContent=String(r.status)};
</script>`;
const encode = value => Buffer.from(JSON.stringify(value)).toString("base64url");
const browser = await chromium.launch({headless:true});
let passed = 0;
async function fixture(options = {}) {
  const context = await browser.newContext({acceptDownloads:true});
  if (options.expiredTransaction) await context.addInitScript(() => {
    if (location.pathname !== "/auth/callback") return;
    const key = "zont.oidc.transaction";
    const transaction = JSON.parse(sessionStorage.getItem(key));
    transaction.created -= 600001;
    sessionStorage.setItem(key, JSON.stringify(transaction));
  });
  const page = await context.newPage();
  const requests = [];
  let authorize;
  let lastCallback;
  let issuedToken;
  let tokenRequests = 0;
  let apiStatus = 200;
  let currentIndex = options.index || index;
  await page.route("**/*", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = await request.allHeaders();
    requests.push({url:url.href, headers});
    if (url.origin === "https://auth.yandex.cloud") {
      assert.equal(headers.authorization, undefined);
      assert.equal(headers.cookie, undefined);
      if (url.pathname === "/oauth/authorize") {
        authorize = url.searchParams;
        assert.equal(authorize.get("response_type"), "code");
        assert.equal(authorize.get("code_challenge_method"), "S256");
        assert.equal(authorize.get("scope"), "openid");
        assert.match(authorize.get("nonce"), /^[A-Za-z0-9_-]{43}$/);
        assert.match(authorize.get("state"), /^[A-Za-z0-9_-]{43}$/);
        const callback = new URL(authorize.get("redirect_uri"));
        callback.search = new URLSearchParams({code:"fixture-code", state:options.badState ? "wrong" : authorize.get("state")});
        lastCallback = callback.href;
        await route.fulfill({contentType:"text/html", body:`<script>location.replace(${JSON.stringify(callback.href)})</script>`});
      } else if (url.pathname === "/oauth/token") {
        tokenRequests++;
        assert.equal(new URL(page.url()).search, "", "callback credentials scrubbed before token request");
        assert.equal(await page.evaluate(() => sessionStorage.getItem("zont.oidc.transaction")), null, "one-time transaction consumed");
        const body = new URLSearchParams(request.postData());
        assert.equal(body.get("client_secret"), null);
        assert.equal(body.get("code"), "fixture-code");
        assert.equal(body.get("redirect_uri"), "https://example.test/auth/callback");
        assert.equal(createHash("sha256").update(body.get("code_verifier")).digest("base64url"), authorize.get("code_challenge"));
        const now = Math.floor(Date.now() / 1000);
        const claims = {iss:"https://auth.yandex.cloud", aud:"public-test-client", sub:"test-subject", iat:now, exp:now+600,
          nonce:authorize.get("nonce"), ...options.claims};
        if (options.badNonce) claims.nonce = "wrong";
        const signingInput = encode({alg:options.algorithm || "RS256", kid:"test-key"}) + "." + encode(claims);
        const signature = sign("RSA-SHA256", Buffer.from(signingInput), privateKey);
        if (options.badSignature) signature[0] ^= 1;
        issuedToken = signingInput + "." + signature.toString("base64url");
        await route.fulfill({contentType:"application/json", body:JSON.stringify({id_token:issuedToken, access_token:"NEVER-STORE-ACCESS", refresh_token:"NEVER-STORE-REFRESH"})});
      } else if (url.pathname === "/oauth/jwks/keys") {
        await route.fulfill({contentType:"application/json", body:JSON.stringify({keys:[jwk, ...Array.from({length:(options.keyCount || 1)-1}, (_, i) => ({kid:`unused-${i}`, kty:"RSA"}))], extra:options.largeKeys ? "x".repeat(65536) : undefined})});
      } else throw Error("Unexpected provider request " + url.pathname);
    } else if (url.origin !== "https://example.test") {
      assert.equal(headers.authorization, undefined, "ID token must never leave protected origin");
      await route.fulfill({body:"external", headers:{"access-control-allow-origin":"*"}});
    } else if (url.pathname === "/reports.json" || url.pathname === "/za/reports.json") {
      assert.equal(headers.authorization, "Bearer " + issuedToken);
      assert.equal(headers.cookie, undefined);
      await route.fulfill({status:options.indexStatus || 200, contentType:"application/json", body:JSON.stringify(currentIndex)});
    } else if (url.pathname.startsWith("/objects/")) {
      assert.equal(headers.authorization, "Bearer " + issuedToken);
      if (options.redirectObject) await route.fulfill({status:302, headers:{location:"https://external.test/stolen"}});
      else await route.fulfill({contentType:url.pathname.endsWith(".json") ? "application/json" : "text/html", body:url.pathname.endsWith(".json") ? '{"immutable":true}' : report});
    } else if (url.pathname.startsWith("/za/api/") || url.pathname.startsWith("/api/")) {
      assert.equal(headers.authorization, "Bearer " + issuedToken);
      await route.fulfill({status:apiStatus, contentType:"application/json", body:"{}"});
    } else {
      assert.equal(headers.authorization, undefined, "public shell receives no token");
      await route.fulfill({contentType:"text/html", body:shell});
    }
  });
  const login = async (path="/daily/2026-09-26.html") => {
    await page.goto("https://example.test" + path);
    await page.locator("#site-login").click();
  };
  return {context,page,requests,login, tokenRequests:()=>tokenRequests,
    lastCallback:()=>lastCallback, apiStatus:value=>apiStatus=value, currentIndex:value=>currentIndex=value};
}
try {
  const f = await fixture();
  await f.login("/za/daily/2026-09-26.html");
  await f.page.locator("#report").waitFor();
  assert.equal(await f.page.evaluate(() => window.shellKeptPath), "/za/daily/2026-09-26.html");
  await f.page.locator("#api").click();
  await f.page.waitForFunction(() => document.querySelector("#api").textContent === "200");
  const stored = await f.page.evaluate(() => ({local:{...localStorage}, session:{...sessionStorage}, cookie:document.cookie}));
  assert.deepEqual(stored, {local:{}, session:{"zont.oidc.continue":"1"}, cookie:""});
  await f.page.evaluate(() => fetch("https://external.test/public"));
  const downloadEvent = f.page.waitForEvent("download");
  await f.page.locator("#json").click();
  const download = await downloadEvent;
  assert.equal(download.suggestedFilename(), "2026-09-26.json");
  assert.equal(readFileSync(await download.path(), "utf8"), '{"immutable":true}');
  await f.page.locator("#archive").click();
  await f.page.locator("#report").waitFor();
  assert.equal(f.tokenRequests(), 2, "native archive navigation resumes provider SSO with fresh PKCE");
  await f.page.locator("#site-logout").click();
  assert.equal(await f.page.locator("#report").count(), 0);
  assert.match(await f.page.locator("#site-status").textContent(), /вышли/);
  await f.page.reload();
  await f.page.locator("#site-login").waitFor();
  assert.equal(f.tokenRequests(), 2, "logout must not immediately resume SSO");
  await f.context.close(); passed++;

  for (const options of [{badState:true}, {badNonce:true}, {badSignature:true}, {algorithm:"none"},
    {claims:{iss:"https://external.test"}}, {claims:{aud:"wrong-client"}}, {claims:{exp:1}}, {claims:{iat:9999999999}}, {claims:{sub:""}}, {keyCount:129}, {largeKeys:true}, {expiredTransaction:true}]) {
    const f = await fixture(options);
    await f.login();
    await f.page.waitForFunction(() => document.querySelector("#site-status")?.textContent.includes("Не удалось"));
    assert.equal(f.requests.some(request => request.url.includes("/reports.json")), false, "invalid login cannot read reports");
    if (options.badState) assert.equal(f.tokenRequests(), 0);
    assert.deepEqual(await f.page.evaluate(() => ({...sessionStorage})), {});
    assert.equal(new URL(f.page.url()).search, "");
    await f.context.close(); passed++;
  }
  for (const options of [{index:{...index, reports:[{...index.reports[0], html_key:"../private.html"}]}},
    {index:{...index, manifest_key:"https://external.test/key"}}, {redirectObject:true}]) {
    const f = await fixture(options);
    await f.login();
    await f.page.waitForFunction(() => document.querySelector("#site-status")?.textContent.includes("недоступен"));
    assert.equal(f.requests.some(request => request.url.startsWith("https://external.test")), false);
    await f.context.close(); passed++;
  }
  const denied = await fixture({indexStatus:401});
  await denied.login();
  await denied.page.waitForFunction(() => document.querySelector("#site-status")?.textContent.includes("Сессия"));
  assert.equal(denied.tokenRequests(), 1);
  await denied.context.close(); passed++;

  const apiDenied = await fixture();
  await apiDenied.login();
  await apiDenied.page.locator("#report").waitFor();
  apiDenied.apiStatus(403);
  await apiDenied.page.locator("#api").click();
  await apiDenied.page.locator("#site-login").waitFor();
  assert.equal(await apiDenied.page.locator("#report").count(), 0);
  await apiDenied.context.close(); passed++;

  const jsonDirect = await fixture();
  await jsonDirect.page.goto("https://example.test/za/daily/2026-09-26.json");
  const directDownload = jsonDirect.page.waitForEvent("download");
  await jsonDirect.page.locator("#site-login").click();
  assert.equal((await directDownload).suggestedFilename(), "2026-09-26.json");
  await jsonDirect.context.close(); passed++;

  for (const keyCount of [35, 128]) {
    const f = await fixture({keyCount});
    await f.login();
    await f.page.locator("#report").waitFor();
    await f.page.goto(f.lastCallback());
    await f.page.waitForFunction(() => document.querySelector("#site-status")?.textContent.includes("Не удалось"));
    assert.equal(f.tokenRequests(), 1, "callback replay must not exchange a second time");
    await f.context.close(); passed++;
  }
  const expiry = await fixture({claims:{exp:Math.floor(Date.now()/1000)+3}});
  await expiry.login();
  await expiry.page.locator("#report").waitFor();
  await expiry.page.locator("#site-login").waitFor();
  assert.equal(await expiry.page.locator("#report").count(), 0, "idle expiry clears protected content");
  await expiry.context.close(); passed++;

  const logoutRoute = await fixture();
  await logoutRoute.login();
  await logoutRoute.page.locator("#report").waitFor();
  await logoutRoute.page.goto("https://example.test/auth/logout");
  await logoutRoute.page.locator("#site-login").waitFor();
  assert.equal(logoutRoute.tokenRequests(), 1);
  assert.deepEqual(await logoutRoute.page.evaluate(() => ({...sessionStorage})), {});
  await logoutRoute.context.close(); passed++;

  console.log(`site_shell: ${passed} browser scenarios passed`);
} finally {
  await browser.close();
}

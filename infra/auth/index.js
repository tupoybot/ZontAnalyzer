'use strict';
// Small OIDC redirect/callback handler; no application, database or user password.
const { randomBytes, createHash, createCipheriv, createDecipheriv, createPublicKey, verify, timingSafeEqual } = require('node:crypto');
const SESSION = '__Host-zont_oidc';
const TRANSACTION = '__Host-zont_login';
const PROVIDER = 'https://auth.yandex.cloud';
const MAX_JWKS_KEYS = 128;
const b64 = value => Buffer.from(value).toString('base64url');
const equal = (a, b) => typeof a === 'string' && typeof b === 'string' &&
  a.length === b.length && timingSafeEqual(Buffer.from(a), Buffer.from(b));
function cookie(name, value, age) {
  return `${name}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${age}`;
}
function cookies(event) {
  const headers = {};
  for (const [key,value] of Object.entries(event.headers || {})) {
    const name=key.toLowerCase();
    if (Object.hasOwn(headers,name)) throw Error('duplicate header');
    headers[name]=value;
  }
  const seen=new Set();
  for (const [key,values] of Object.entries(event.multiValueHeaders || {})) {
    const name=key.toLowerCase();
    if (!['cookie','origin','sec-fetch-site'].includes(name)) continue;
    if (seen.has(name) || !Array.isArray(values) || !values.length || values.length>32 ||
        values.some(value=>typeof value!=='string') || values.reduce((size,value)=>size+value.length,0)>8192 ||
        (name!=='cookie' && values.length!==1)) throw Error('duplicate header');
    seen.add(name);
    headers[name]=values.join(name==='cookie'?'; ':'');
  }
  const raw = headers.cookie || '';
  if (typeof raw !== 'string' || raw.length > 8192) throw Error('cookie');
  const result = {};
  for (const part of raw.split(';')) {
    const at = part.indexOf('=');
    if (at < 0) continue;
    const name = part.slice(0, at).trim();
    if (name !== SESSION && name !== TRANSACTION) continue;
    if (Object.hasOwn(result, name)) throw Error('duplicate cookie');
    result[name] = part.slice(at + 1).trim();
  }
  return {headers, values: result};
}
function seal(value, key) {
  const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv);
  const body = Buffer.concat([cipher.update(JSON.stringify(value), 'utf8'), cipher.final()]);
  return b64(Buffer.concat([iv, cipher.getAuthTag(), body]));
}
function unseal(value, key) {
  if (!value || value.length > 2048 || !/^[\w-]+$/.test(value)) throw Error('transaction');
  const data = Buffer.from(value, 'base64url');
  const decipher = createDecipheriv('aes-256-gcm', key, data.subarray(0, 12));
  decipher.setAuthTag(data.subarray(12, 28));
  return JSON.parse(Buffer.concat([decipher.update(data.subarray(28)), decipher.final()]));
}
function config(env) {
  if (typeof env.OIDC_PUBLIC_ORIGIN!=='string' || env.OIDC_PUBLIC_ORIGIN.length>2048 ||
      typeof env.OIDC_TRANSACTION_KEY!=='string' || env.OIDC_TRANSACTION_KEY.length>128 ||
      typeof env.OIDC_CLIENT_SECRET!=='string' || env.OIDC_CLIENT_SECRET.length>4096) throw Error('config');
  const origin = new URL(env.OIDC_PUBLIC_ORIGIN);
  const key = Buffer.from(env.OIDC_TRANSACTION_KEY || '', 'base64url');
  if (origin.protocol !== 'https:' || origin.origin !== env.OIDC_PUBLIC_ORIGIN || key.length !== 32 ||
      !/^[a-zA-Z0-9_-]{3,128}$/.test(env.OIDC_CLIENT_ID || '') || !env.OIDC_CLIENT_SECRET ||
      ![PROVIDER, `${PROVIDER}/oauth/${env.OIDC_CLIENT_ID}`].includes(env.OIDC_ISSUER)) throw Error('config');
  return {origin: origin.origin, key, client: env.OIDC_CLIENT_ID, secret: env.OIDC_CLIENT_SECRET, issuer: env.OIDC_ISSUER};
}
async function boundedJSON(fetcher, url, options = {}) {
  const response = await fetcher(url, {...options, redirect: 'error', signal: AbortSignal.timeout(5000)});
  if (!response.ok) throw Error('provider');
  let size = 0; const chunks = [];
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > 65536) throw Error('provider response');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
function verifyToken(token, jwks, cfg, nonce, now) {
  if (typeof token !== 'string' || token.length > 3500 || !/^[\w-]+\.[\w-]+\.[\w-]+$/.test(token)) throw Error('token');
  const [head, body, sig] = token.split('.');
  const header = JSON.parse(Buffer.from(head, 'base64url'));
  const claims = JSON.parse(Buffer.from(body, 'base64url'));
  if (header.alg !== 'RS256' || ['crit','jku','jwk','x5u'].some(name=>Object.hasOwn(header,name)) ||
      typeof header.kid !== 'string' || !/^[\w-]{1,128}$/.test(header.kid) ||
      !Array.isArray(jwks.keys) || jwks.keys.length > MAX_JWKS_KEYS) throw Error('algorithm');
  const keys = jwks.keys.filter(k => k.kid === header.kid && k.kty === 'RSA' && (!k.use || k.use === 'sig') && (!k.alg || k.alg === 'RS256'));
  if (keys.length !== 1) throw Error('key');
  const key = createPublicKey({key: keys[0], format: 'jwk'});
  if (key.asymmetricKeyDetails.modulusLength < 2048 || key.asymmetricKeyDetails.modulusLength > 8192 ||
      !verify('RSA-SHA256', Buffer.from(`${head}.${body}`), key, Buffer.from(sig, 'base64url'))) throw Error('signature');
  if (claims.iss !== cfg.issuer || claims.aud !== cfg.client || !equal(claims.nonce, nonce) ||
      typeof claims.sub !== 'string' || !claims.sub || claims.sub.length>256 ||
      !Number.isSafeInteger(claims.exp) || !Number.isSafeInteger(claims.iat) || claims.exp<=claims.iat ||
      claims.exp <= now || claims.iat > now + 30 || claims.iat < now - 600 ||
      (claims.nbf !== undefined && (!Number.isSafeInteger(claims.nbf) || claims.nbf > now + 30)) ||
      (claims.azp !== undefined && claims.azp !== cfg.client)) throw Error('claims');
  return claims;
}
function response(statusCode, body = '', extra = {}, setCookies = []) {
  return {statusCode, headers: {'Content-Type':'text/html; charset=utf-8', 'Cache-Control':'no-store',
    'Referrer-Policy':'no-referrer', 'X-Content-Type-Options':'nosniff',
    'Content-Security-Policy':"default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'", ...extra},
    multiValueHeaders: setCookies.length ? {'Set-Cookie': setCookies} : {}, body, isBase64Encoded:false};
}
function createHandler(env = process.env, fetcher = fetch, clock = () => Math.floor(Date.now()/1000)) {
  return async event => {
    try {
      const cfg = config(env), now = clock();
      const {headers, values} = cookies(event);
      const path = event.path || event.rawPath;
      const method = event.httpMethod;
      const query = event.queryStringParameters || {};
      for (const values of Object.values(event.multiValueQueryStringParameters || {})) if (values.length !== 1) throw Error('query');
      if (path === '/auth/login' && method === 'GET') {
        const state = b64(randomBytes(32)), nonce = b64(randomBytes(32)), verifier = b64(randomBytes(32));
        const target = new URL(`${PROVIDER}/oauth/authorize`);
        target.search = new URLSearchParams({client_id:cfg.client, redirect_uri:cfg.origin+'/auth/callback',
          response_type:'code', scope:'openid profile', state, nonce, prompt:'login',
          code_challenge:b64(createHash('sha256').update(verifier).digest()), code_challenge_method:'S256'}).toString();
        return response(303, '', {Location:target.href}, [cookie(TRANSACTION, seal({state,nonce,verifier,exp:now+600},cfg.key),600)]);
      }
      if (path === '/auth/callback' && method === 'GET') {
        const tx = unseal(values[TRANSACTION], cfg.key);
        if (!Number.isInteger(tx.exp) || tx.exp <= now || tx.exp > now+600 || !equal(query.state,tx.state) ||
            typeof query.code !== 'string' || !query.code || query.code.length > 2048 || query.error ||
            (query.iss && query.iss !== cfg.issuer)) throw Error('callback');
        const tokens = await boundedJSON(fetcher, `${PROVIDER}/oauth/token`, {method:'POST',
          headers:{'Content-Type':'application/x-www-form-urlencoded', Authorization:'Basic '+Buffer.from(`${encodeURIComponent(cfg.client)}:${encodeURIComponent(cfg.secret)}`).toString('base64')},
          body:new URLSearchParams({grant_type:'authorization_code',code:query.code,redirect_uri:cfg.origin+'/auth/callback',code_verifier:tx.verifier}).toString()});
        const jwks = await boundedJSON(fetcher, `${PROVIDER}/oauth/jwks/keys`);
        const checkedAt=clock();
        const claims = verifyToken(tokens.id_token, jwks, cfg, tx.nonce, checkedAt);
        return response(303, '', {Location:'/'}, [cookie(TRANSACTION,'',0), cookie(SESSION,tokens.id_token,claims.exp-checkedAt)]);
      }
      if (path === '/auth/logout' && method === 'POST') {
        if (headers.origin !== cfg.origin || headers['sec-fetch-site'] === 'cross-site') return response(403, 'Запрос отклонён.');
        return response(200, '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Выход</title><p>Вы вышли из отчётов.</p><a href="/auth/login">Войти снова</a></html>', {}, [cookie(SESSION,'',0),cookie(TRANSACTION,'',0)]);
      }
      return response(404, 'Страница не найдена.');
    } catch {
      // Never log incoming events, auth codes, cookies or provider errors.
      return response(400, '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Вход</title><p>Не удалось завершить вход.</p><a href="/auth/login">Попробовать снова</a></html>', {}, [cookie(TRANSACTION,'',0)]);
    }
  };
}
module.exports = {handler:createHandler(), createHandler, verifyToken};

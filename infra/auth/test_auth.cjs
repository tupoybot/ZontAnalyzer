'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {generateKeyPairSync, sign, randomBytes} = require('node:crypto');
const {createHandler} = require('./index.js');
const env = {OIDC_PUBLIC_ORIGIN:'https://reports.example.test',OIDC_CLIENT_ID:'test-client',OIDC_CLIENT_SECRET:'test-secret',
  OIDC_ISSUER:'https://auth.yandex.cloud',OIDC_TRANSACTION_KEY:randomBytes(32).toString('base64url')};
const now = 1800000000;
const {privateKey,publicKey} = generateKeyPairSync('rsa',{modulusLength:2048});
const jwks = {keys:[{...publicKey.export({format:'jwk'}),kid:'test',alg:'RS256',use:'sig'}]};
function token(claims, key=privateKey, header={alg:'RS256',kid:'test'}) {
  const input=[header,claims].map(v=>Buffer.from(JSON.stringify(v)).toString('base64url')).join('.');
  return `${input}.${sign('RSA-SHA256',Buffer.from(input),key).toString('base64url')}`;
}
async function fixture(change={}, signature={}) {
  const requests=[];let nonce;
  const fetcher=async(url,options)=>{
    requests.push({url,options});
    return new Response(JSON.stringify(url.endsWith('/token')?{id_token:token({iss:env.OIDC_ISSUER,aud:env.OIDC_CLIENT_ID,
      sub:'local-reviewer',iat:now,exp:now+3600,nonce,...change},signature.key,signature.header)}:signature.jwks||jwks));
  };
  const handler=createHandler(env,fetcher,()=>now);
  const login=await handler({path:'/auth/login',httpMethod:'GET'});
  assert.equal(login.statusCode,303);
  const location=new URL(login.headers.Location);nonce=location.searchParams.get('nonce');
  const txCookie=login.multiValueHeaders['Set-Cookie'][0].split(';')[0];
  const event={path:'/auth/callback',httpMethod:'GET',headers:{Cookie:txCookie},
    queryStringParameters:{code:'one-time-code',state:location.searchParams.get('state')}};
  return {handler,login,location,event,requests};
}
test('PKCE login, bound callback and HttpOnly signed session; tokens never enter HTML',async()=>{
  const f=await fixture();const r=await f.handler(f.event);
  assert.equal(r.statusCode,303);assert.equal(r.headers.Location,'/');
  const cookies=r.multiValueHeaders['Set-Cookie'];assert.equal(cookies.length,2);
  assert.match(cookies[1],/^__Host-zont_oidc=.*; Path=\/; Secure; HttpOnly; SameSite=Lax; Max-Age=3600$/);
  const verifier=new URLSearchParams(f.requests[0].options.body).get('code_verifier');
  const crypto=require('node:crypto');
  assert.equal(crypto.createHash('sha256').update(verifier).digest('base64url'),f.location.searchParams.get('code_challenge'));
  assert.equal(f.requests[0].options.redirect,'error');assert.equal(r.body,'');
});
test('state mismatch, cookie tamper and duplicate cookies rejected before token endpoint',async()=>{
  for(const kind of ['state','tamper','duplicate','multiquery']) {
    const f=await fixture();
    if(kind==='state')f.event.queryStringParameters.state='attacker';
    if(kind==='tamper')f.event.headers.Cookie+='a';
    if(kind==='duplicate')f.event.headers.Cookie+='; '+f.event.headers.Cookie;
    if(kind==='multiquery')f.event.multiValueQueryStringParameters={state:['x','y']};
    assert.equal((await f.handler(f.event)).statusCode,400);assert.equal(f.requests.length,0);
  }
});
test('wrong nonce, issuer, audience, time and malformed token claims fail closed',async()=>{
  for(const change of [{nonce:'attacker'},{iss:'https://attacker.test'},{aud:'other'},{exp:now},{iat:now+300},{sub:null},{nbf:now+120},{azp:'other'}]) {
    const f=await fixture(change);const r=await f.handler(f.event);assert.equal(r.statusCode,400);assert.ok(!JSON.stringify(r).includes('one-time-code'));
  }
});
test('provider failure is generic, clears transaction and does not set session',async()=>{
  const f=await fixture();const handler=createHandler(env,async()=>{throw Error('SECRET')},()=>now);
  const r=await handler(f.event);assert.equal(r.statusCode,400);assert.ok(!JSON.stringify(r).includes('SECRET'));
  assert.ok(!r.multiValueHeaders['Set-Cookie'].some(c=>c.startsWith('__Host-zont_oidc=')));
});
test('logout requires same-origin POST and clears both HttpOnly cookies',async()=>{
  const handler=createHandler(env,()=>{throw Error('unexpected fetch')},()=>now);
  assert.equal((await handler({path:'/auth/logout',httpMethod:'POST',headers:{Origin:'https://attacker.test'}})).statusCode,403);
  const r=await handler({path:'/auth/logout',httpMethod:'POST',headers:{Origin:env.OIDC_PUBLIC_ORIGIN}});
  assert.equal(r.statusCode,200);assert.equal(r.multiValueHeaders['Set-Cookie'].length,2);
  assert.ok(r.multiValueHeaders['Set-Cookie'].every(c=>c.endsWith('Max-Age=0')));
});
test('missing and unsafe configuration never redirects to arbitrary destinations',async()=>{
  for(const change of [{OIDC_PUBLIC_ORIGIN:'http://reports.example.test'},{OIDC_TRANSACTION_KEY:'short'},{OIDC_ISSUER:'https://evil.test'}]){
    const r=await createHandler({...env,...change})({path:'/auth/login',httpMethod:'GET'});assert.equal(r.statusCode,400);
  }
});
test('callback rejects wrong signature, algorithm and external signing-key headers',async()=>{
  const {privateKey:other}=generateKeyPairSync('rsa',{modulusLength:2048});
  for(const signature of [{key:other},{header:{alg:'none',kid:'test'}},{header:{alg:'HS256',kid:'test'}},
    {header:{alg:'RS256',kid:'unknown'}},{header:{alg:'RS256',kid:'test',jku:'https://attacker.test/keys'}}]){
    const f=await fixture({},signature);const r=await f.handler(f.event);
    assert.equal(r.statusCode,400);
    assert.ok(!r.multiValueHeaders['Set-Cookie'].some(value=>value.startsWith('__Host-zont_oidc=')));
  }
});
test('expired, missing, foreign-browser and rotated-key transactions never reach provider',async()=>{
  for(const kind of ['expired','missing','foreign','rotated','empty-code']){
    const f=await fixture();let requests=0;
    const changedEnv=kind==='rotated'?{...env,OIDC_TRANSACTION_KEY:randomBytes(32).toString('base64url')}:env;
    const handler=createHandler(changedEnv,async()=>{requests++;throw Error('unexpected')},()=>now+(kind==='expired'?601:0));
    if(kind==='missing')delete f.event.headers.Cookie;
    if(kind==='foreign')f.event.headers.Cookie=(await fixture()).event.headers.Cookie;
    if(kind==='empty-code')f.event.queryStringParameters.code='';
    assert.equal((await handler(f.event)).statusCode,400);assert.equal(requests,0);
  }
});
test('multi-value and case-duplicated security cookies fail closed; distinct cookie crumbs work',async()=>{
  for(const kind of ['multi-cookie','case-cookie']){
    const f=await fixture();
    if(kind==='multi-cookie')f.event.multiValueHeaders={Cookie:[f.event.headers.Cookie,f.event.headers.Cookie]};
    if(kind==='case-cookie')f.event.headers.cookie=f.event.headers.Cookie;
    assert.equal((await f.handler(f.event)).statusCode,400);assert.equal(f.requests.length,0);
  }
  const f=await fixture();f.event.multiValueHeaders={Cookie:['unrelated=value',f.event.headers.Cookie]};
  assert.equal((await f.handler(f.event)).statusCode,303);
});
test('unsafe timestamps and token expiry during provider I/O cannot produce a session',async()=>{
  for(const change of [{exp:Number.MAX_SAFE_INTEGER+1},{exp:now+5,iat:now+10},{sub:'x'.repeat(257)}]){
    const f=await fixture(change);assert.equal((await f.handler(f.event)).statusCode,400);
  }
  const f=await fixture();let clock=now;
  const handler=createHandler(env,async url=>{
    const value=url.endsWith('/token')?{id_token:token({iss:env.OIDC_ISSUER,aud:env.OIDC_CLIENT_ID,
      sub:'local-reviewer',iat:now,exp:now+1,nonce:f.location.searchParams.get('nonce')})}:jwks;
    clock=now+2;
    return new Response(JSON.stringify(value));
  },()=>clock);
  assert.equal((await handler(f.event)).statusCode,400);
});
test('oversized and redirect provider responses remain generic; oversized config is rejected',async()=>{
  for(const providerResponse of [()=>new Response('private-body'.repeat(6000)),
    ()=>new Response('',{status:302,headers:{Location:'https://attacker.test'}})]){
    const f=await fixture();
    const handler=createHandler(env,async(_url,options)=>{
      assert.equal(options.redirect,'error');assert.ok(options.signal);return providerResponse();
    },()=>now);
    const r=await handler(f.event);assert.equal(r.statusCode,400);
    assert.ok(!JSON.stringify(r).includes('private-body'));
    assert.ok(!r.multiValueHeaders['Set-Cookie'].some(value=>value.startsWith('__Host-zont_oidc=')));
  }
  const r=await createHandler({...env,OIDC_CLIENT_SECRET:'x'.repeat(4097)})({path:'/auth/login',httpMethod:'GET'});
  assert.equal(r.statusCode,400);
});
test('logout rejects missing and ambiguous Origin; GET never logs out',async()=>{
  const handler=createHandler(env,()=>{throw Error('unexpected fetch')},()=>now);
  assert.equal((await handler({path:'/auth/logout',httpMethod:'POST'})).statusCode,403);
  assert.equal((await handler({path:'/auth/logout',httpMethod:'GET',headers:{Origin:env.OIDC_PUBLIC_ORIGIN}})).statusCode,404);
  const r=await handler({path:'/auth/logout',httpMethod:'POST',headers:{Origin:env.OIDC_PUBLIC_ORIGIN},
    multiValueHeaders:{Origin:[env.OIDC_PUBLIC_ORIGIN,'https://attacker.test']}});
  assert.equal(r.statusCode,400);
  assert.ok(!r.multiValueHeaders['Set-Cookie'].some(value=>value.startsWith('__Host-zont_oidc=')));
});
test('large public-key rotation history selects the last key and keeps the 128-key bound',async()=>{
  const {publicKey:previous}=generateKeyPairSync('rsa',{modulusLength:2048});
  const retired={...previous.export({format:'jwk'}),alg:'RS256',use:'sig'};
  for(const count of [35,128,129]){
    const document={keys:[...Array.from({length:count-1},(_,index)=>({...retired,kid:`previous-${index}`})),jwks.keys[0]]};
    assert.ok(Buffer.byteLength(JSON.stringify(document))<65536);
    const f=await fixture({}, {jwks:document});
    const result=await f.handler(f.event);
    assert.equal(result.statusCode,count<=128?303:400);
  }
});

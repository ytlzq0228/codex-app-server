const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
async function testRelogin(provider) {
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {hidden:false, value:'', textContent:'', listeners:{},
    addEventListener(name, handler) { this.listeners[name] = handler; },
    querySelector(selector) { return element(id+selector); },
    setAttribute(){}, removeAttribute(){}, replaceChildren(){}, append(){}, showModal(){}, close(){}});
  return elements.get(id);
}
const form = element('form'); form.dataset = {provider, workerId:'123'};
class FormData {
  constructor(form) { this.values = form ? {force:'true', csrf_token:'test'} : {}; }
  get(key) { return this.values[key]; } set(key,value) {this.values[key]=value;}
}
const actions = [];
let failures = 2;
const context = {I18n:{ready:Promise.resolve(),t:name=>name}, document:{getElementById:element, querySelector:element, querySelectorAll:()=>[form]},
  FormData, URL, URLSearchParams, confirm:()=>true, location:{reload(){}}, setTimeout(){}, clearTimeout(){},
  fetch:async url => {
    const action = url.split('/').pop(); actions.push(action);
    if (action === 'logout' && failures-- > 0) return {ok:false,status:503,json:async()=>({detail:'offline'})};
    return {ok:true,json:async()=>action==='start' ? {session_id:'session',error:'test ends here'} : {}};
  }};
await vm.runInNewContext(fs.readFileSync('src/codex_gateway/static/gemini-login.js','utf8'),context);

  await form.listeners.submit({preventDefault(){}});
  assert.deepEqual(actions,['logout']);
  await element('provider-login-retry').listeners.click();
  assert.deepEqual(actions,['logout','logout']);
  await element('provider-login-retry').listeners.click();
  assert.deepEqual(actions,['logout','logout','logout','start']);
}
(async () => {
  for (const provider of ['gemini', 'claude']) await testRelogin(provider);
  console.log('Gemini and Claude relogin retry: passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

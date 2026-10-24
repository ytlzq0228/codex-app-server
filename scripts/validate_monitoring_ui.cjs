// Run from the repository root with PLAYWRIGHT_MODULE pointing to an installed Playwright.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs'),assert=require('node:assert/strict');
(async()=>{
const browser=await chromium.launch({executablePath:process.env.CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
const page=await browser.newPage(); const errors=[],calls=[];
page.on('pageerror',e=>errors.push(e.message));
const ids=['monitoring','monitor-status','monitor-worker-total','monitor-pie','monitor-state-chart',...['codex','gemini','claude'].flatMap(p=>['monitor-usage-'+p,'monitor-chart-'+p])];
const html='<select id="monitor-days"><option>1</option><option selected>7</option><option>30</option><option>90</option></select>'+ids.map(id=>'<div id="'+id+'"></div>').join('');
const at=new Date().toISOString();
const pool={eligible:1,total_weight:1,unknown_plans:0,windows:Object.fromEntries(['risk','five_hour','week'].map(k=>[k,{used:50,min:10,max:90,covered:1,covered_weight:1}]))};
const usage={providers:{codex:pool,gemini:pool,claude:pool}};
const states={total:1,counts:{healthy:1}};
let release; const gate=new Promise(r=>release=r); let first=true;
await page.route('https://monitor.test/**',async route=>{
const u=new URL(route.request().url());
if(u.pathname==='/')return route.fulfill({body:html,contentType:'text/html'});
calls.push(u);
if(u.searchParams.get('view')==='summary')return route.fulfill({json:{states,labels:{healthy:'正常'},current_usage:{observed_at:at,data:usage}}});
if(first){first=false;await gate;}
const row=data=>({at,observed_at:at,data});
return route.fulfill({json:{server_time:at,intervals:{worker_states:1800000,subscription_usage:1800000},replace_from:{worker_states:at,subscription_usage:at},history:{worker_states:[row(states)],subscription_usage:[row(usage)]}}});
});
await page.goto('https://monitor.test/');
await page.evaluate(()=>{globalThis.I18n={ready:Promise.resolve(),locale:'zh-CN',t:s=>s};});
await page.addScriptTag({content:fs.readFileSync('src/codex_gateway/static/monitoring.js','utf8')});
await page.waitForFunction(()=>document.querySelector('#monitor-worker-total').textContent==='1');
assert.equal(await page.locator('#monitor-state-chart svg').count(),0);
release();
await page.waitForSelector('#monitor-state-chart svg');
assert.equal(await page.locator('circle').count(),10);
await page.locator('#monitor-chart-codex svg').hover();
await page.waitForFunction(()=>document.querySelector('#monitor-chart-codex .monitor-tooltip').textContent.includes('范围'));
await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
await page.waitForFunction(()=>document.querySelector('#monitor-status').textContent.includes('状态更新'));
await page.waitForTimeout(100);
assert(calls.some(u=>u.searchParams.has('since')));
await page.selectOption('#monitor-days','30');
await page.waitForTimeout(100);
assert(calls.some(u=>u.searchParams.get('days')==='30'&&!u.searchParams.has('since')));
assert.deepEqual(errors,[]);
console.log('PASS: independent summary, chart rendering, hover, incremental request, range reset; no browser errors');
await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});

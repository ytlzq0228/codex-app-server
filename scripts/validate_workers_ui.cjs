/** Offline browser checks. PYTHON=.venv/bin/python PLAYWRIGHT_MODULE=/path/to/playwright node scripts/validate_workers_ui.cjs */
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const packageRoot = path.join(root, 'src/codex_gateway');
const html = execFileSync(process.env.PYTHON || 'python3', ['-c', `
from types import SimpleNamespace as S
from pathlib import Path
from jinja2 import Environment,FileSystemLoader
from codex_gateway.display import money,tokens
from codex_gateway.subscriptions import plan_pill_style,DEFAULT_PLAN_COLOR
root=Path('src/codex_gateway')
env=Environment(loader=FileSystemLoader(root/'templates'),autoescape=True)
env.filters.update(money=money,tokens=tokens)
workers=[]
for i,provider in enumerate(['codex','gemini','claude']):
 workers.append(S(id=provider,name='sys-worker-1' if i==0 else 'long-worker-name-that-must-be-truncated-in-one-line',provider=provider,node_id='app-1' if i!=1 else 'app-2',owner_username='long-owner-name',status=S(value='error' if i==1 else 'ready'),enabled=True,account_email='long-address-that-must-not-expand-the-table@example.test',auth_mode=provider+'-subscription',plan_type='team',failure_kind='connection' if i==1 else None,failure_reason='Connection timeout: a deliberately long reason for truncation' if i==1 else 'Historical error must not appear',recovered_at='historical recovery'))
print(env.get_template('admin/dashboard.html').render(page='admin_workers',workers=workers,users=[],csrf_token='test',request=S(state=S(user=S(role='admin'))),plan_styles={},default_plan_style=plan_pill_style(DEFAULT_PLAN_COLOR)))
`], {cwd:root,encoding:'utf8'});
(async () => {
 const browser = await chromium.launch({executablePath:process.env.CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
 const page = await browser.newPage({viewport:{width:1440,height:900}});
 const errors=[]; page.on('pageerror',error=>errors.push(error.message));
 await page.route('https://gateway.test/**', route => {
  const url=new URL(route.request().url());
  if(url.pathname==='/admin/workers')return route.fulfill({body:html,contentType:'text/html'});
  if(url.pathname.startsWith('/static/'))return route.fulfill({path:path.join(packageRoot,'static',url.pathname.slice(8)),contentType:url.pathname.endsWith('.js')?'text/javascript':'text/css'});
  if(url.pathname.endsWith('/rate-limits')){
   if(url.pathname.includes('/gemini/'))return route.fulfill({json:{unlimited:true}});
   return route.fulfill({json:{buckets:[{five_hour:url.pathname.includes('/claude/')?{used:38,resets_at:1790964600}:null,week:{used:72,resets_at:1791051000}}]}});
  }
  return route.fulfill({status:404,body:'Not found'});
 });
 await page.goto('https://gateway.test/admin/workers');
 await page.locator('.usage-meter').nth(3).waitFor();
 assert.equal(await page.locator('th').count(),8);
 assert.equal(await page.locator('form[action$="/provider-login/logout"]').count(),0);
 assert.equal(await page.locator('.worker-current-error').count(),1);
 assert(!await page.locator('.worker-table').getByText('已自动恢复').count());
 assert(!await page.locator('.worker-table').getByText('Historical error must not appear').count());
 for(const provider of ['chatgpt','gemini','claude']) assert(await page.locator('.badge').filter({hasText:'已登录·'+provider}).count());
 assert.equal(await page.locator('form[data-provider="claude"] input[name="force"]').inputValue(),'true');
 const colors=await page.locator('.worker-node').evaluateAll(nodes=>nodes.map(n=>getComputedStyle(n).backgroundColor));
 assert.notEqual(colors[0],colors[1]);
 for(const width of [1920,1440,1280,1024,390]){
  await page.setViewportSize({width,height:900});
  const layout=await page.evaluate(()=>{
   const wrap=document.querySelector('#workers .table-wrap');
   const name=document.querySelector('.worker-name-text strong');
   const product=document.querySelector('.worker-name-text small');
   const meterWidths=[...document.querySelectorAll('.worker-table tbody tr')].map(row=>[...row.querySelectorAll('.usage-meter')].map(n=>n.getBoundingClientRect().width));
   const actionRows=[...document.querySelectorAll('.worker-table .row-actions')].map(row=>[...row.querySelectorAll('button')].map(n=>n.getBoundingClientRect().top));
   return {buttonHeights:[...document.querySelectorAll('.worker-table .row-actions button')].map(n=>n.getBoundingClientRect().height),meterWidths,actionRows,documentWidth:document.documentElement.scrollWidth,viewport:innerWidth,tableWidth:wrap.scrollWidth,containerWidth:wrap.clientWidth,rows:[...document.querySelectorAll('.worker-table tbody tr')].map(n=>n.getBoundingClientRect().height),authFits:[...document.querySelectorAll('.worker-table td:nth-child(6) .badge')].every(n=>n.scrollWidth<=n.clientWidth),nameFits:name.scrollWidth<=name.clientWidth,nameTop:name.getBoundingClientRect().top,productTop:product.getBoundingClientRect().top,fontSize:getComputedStyle(name).fontSize,resetsInside:[...document.querySelectorAll('.usage-reset')].every(n=>n.closest('.usage-meter')),captions:[...document.querySelectorAll('.usage-caption')].map(n=>n.children.length)};
  });
  assert(layout.documentWidth<=layout.viewport,JSON.stringify({width,...layout}));
  assert(layout.tableWidth<=layout.containerWidth+1,JSON.stringify({width,...layout}));
  assert(layout.rows.every(height=>height<=62),JSON.stringify({width,...layout}));
  assert(layout.productTop>layout.nameTop);
  assert.equal(layout.fontSize,'14px');
  assert(layout.resetsInside && layout.captions.every(count=>count===1 || count===2));
  assert(Math.abs(layout.meterWidths[0][0]-layout.meterWidths[1][0])<1);
  assert(Math.abs(layout.meterWidths[0][0]-(layout.meterWidths[2][0]+layout.meterWidths[2][1]+6))<1);
  assert(layout.meterWidths.flat().every(w=>w<=205));
  assert(layout.buttonHeights.every(h=>h===30));
  assert(layout.actionRows.every(tops=>tops.every(top=>Math.abs(top-tops[0])<1)));
  if(width===1440){assert(layout.nameFits);assert(layout.authFits);}
  if(width===1440 || width===1024)await page.screenshot({path:'/tmp/workers-ui-'+width+'.png',fullPage:true});
 }
 assert.deepEqual(errors,[]);
 console.log('Worker UI passed: column fit at 1920/1440/1280/1024/390, two-line rows, status, node colors, login and usage controls');
 await browser.close();
})().catch(error=>{console.error(error);process.exit(1);});

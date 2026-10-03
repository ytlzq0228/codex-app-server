/** Offline browser checks: PYTHON=.venv/bin/python PLAYWRIGHT_MODULE=/path/to/playwright node scripts/validate_history_ui.cjs */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {execFileSync}=require('node:child_process');
const path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const html=execFileSync(process.env.PYTHON || 'python3',['-c',`
from types import SimpleNamespace as S
from pathlib import Path
from jinja2 import Environment,FileSystemLoader
from codex_gateway.display import money,tokens
root=Path('src/codex_gateway')
e=Environment(loader=FileSystemLoader(root/'templates'),autoescape=True)
e.filters.update(money=money,tokens=tokens)
print(e.get_template('admin/dashboard.html').render(page='history',history_keys=[],csrf_token='test',request=S(query_params={},state=S(user=S(role='admin')))))
`],{cwd:root,encoding:'utf8'});
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const calls=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 let failNext=false;
 await page.route('https://gateway.test/**',async route=>{
  const url=new URL(route.request().url());
  if(url.pathname==='/admin/history')return route.fulfill({body:html,contentType:'text/html'});
  if(url.pathname.startsWith('/static/'))return route.fulfill({path:path.join(root,'src/codex_gateway/static',url.pathname.slice(8)),contentType:url.pathname.endsWith('.js')?'text/javascript':'text/css'});
  calls.push(url);
  if(url.pathname==='/admin/history/data'){
   if(failNext){failNext=false;return route.fulfill({status:500,json:{detail:'测试加载失败'}});}
   const number=Number(url.searchParams.get('history_page') || 1);
   return route.fulfill({json:{total:31,request_total:55,page:number,pages:2,page_size:30,groups:[{identity:'key:responses:conversation-'+number,conversation_id:'conversation-'+number,key_id:'key',key_name:'<img src=x onerror="window.pwned=1">',endpoint:'responses',latest_at:'2026-10-02T15:00:00Z',logical:true,thread_count:2,request_count:25,latest_status:200,input_tokens:10,output_tokens:20,duration_ms:30,cost_usd:'0.1000',unpriced_count:0}]}});
  }
  if(url.pathname==='/admin/history/requests'){
   const number=Number(url.searchParams.get('page') || 1);
   return route.fulfill({json:{total:25,page:number,pages:2,page_size:20,requests:Array.from({length:number===1?20:5},(_,i)=>({request_id:'request-'+((number-1)*20+i),created_at:'2026-10-02T15:00:00Z',owner_username:'owner',model:'<script>window.pwned=1</script>',worker_name:'Worker',thread_id:'thread',evidence:'test',status_code:200,input_tokens:1,output_tokens:2,duration_ms:3,cost_usd:'0.0040'}))}});
  }
  if(url.pathname.startsWith('/user/usage/'))return route.fulfill({contentType:'text/html',body:'<main class="content"><section class="panel">请求详情测试</section></main>'});
  return route.fulfill({status:404,body:'Not found'});
 });
 await page.goto('https://gateway.test/admin/history');
 await page.getByText('共 55 条请求，聚合为 31 个会话').waitFor();
 assert.equal(calls.filter(u=>u.pathname==='/admin/history/requests').length,0);
 assert.equal(await page.locator('[data-history-request]').count(),0);
 await page.getByRole('button',{name:'展开',exact:true}).click();
 await page.locator('[data-history-request]').nth(19).waitFor();
 assert.equal(await page.locator('[data-history-request]').count(),20);
 await page.getByRole('button',{name:'更多（剩余 5 条）'}).click();
 await page.locator('[data-history-request]').nth(24).waitFor();
 assert.equal(calls.filter(u=>u.pathname==='/admin/history/requests').length,2);
 await page.locator('[data-request-detail]').first().click();
 await page.locator('#request-detail-content').getByText('请求详情测试').waitFor();
 await page.locator('#request-detail-dialog [data-modal-close]').click();
 assert.equal(await page.evaluate(()=>window.pwned),undefined);
 await page.getByRole('button',{name:'下一页',exact:true}).click();
 await page.locator('code').getByText('conversation-2',{exact:true}).waitFor();
 assert.equal(await page.locator('[data-history-request]').count(),0);
 assert.equal(calls.filter(u=>u.pathname==='/admin/history/requests').length,2);
 await page.locator('[name="conversation"]').fill('filter-conversation');
 await page.locator('[data-time-bound="start"]').fill('2026-10-02T08:00');
 await page.getByRole('button',{name:'查询',exact:true}).click();
 await page.locator('code').getByText('conversation-1',{exact:true}).waitFor();
 const last=calls.filter(u=>u.pathname==='/admin/history/data').at(-1);
 assert.equal(last.searchParams.get('history_page'),'1');
 assert.equal(last.searchParams.get('conversation'),'filter-conversation');
 assert(last.searchParams.get('start').endsWith('Z'));
 failNext=true;
 await page.getByRole('button',{name:'查询',exact:true}).click();
 await page.locator('[data-history-error]').getByText('测试加载失败').waitFor();
 await page.getByRole('button',{name:'查询',exact:true}).click();
 await page.getByText('共 55 条请求，聚合为 31 个会话').waitFor();
 assert.deepEqual(errors,[]);
 await page.screenshot({path:'/tmp/admin-history-json.png',fullPage:true});
 await browser.close();
 console.log('History UI passed: JSON pagination, lazy request batches, filters, timezone, escaped text, detail modal, error retry');
})().catch(e=>{console.error(e);process.exit(1);});

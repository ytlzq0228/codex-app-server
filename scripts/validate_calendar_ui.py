"""Offline checks for localized calendars and compact navigation; requires Playwright."""
from pathlib import Path
from types import SimpleNamespace as S
from urllib.parse import urlparse
from jinja2 import Environment,FileSystemLoader,select_autoescape
from playwright.sync_api import sync_playwright
from codex_gateway.i18n import t
root=Path(__file__).resolve().parents[1]/'src/codex_gateway'
env=Environment(loader=FileSystemLoader(root/'templates'),autoescape=select_autoescape())
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
 page=browser.new_page(viewport={'width':1366,'height':768},locale='en-US')
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  path=urlparse(r.request.url).path
  if path.startswith('/static/'):
   file=root/path[1:]
   kinds={'.js':'text/javascript','.css':'text/css','.json':'application/json','.svg':'image/svg+xml'}
   return r.fulfill(body=file.read_bytes(),content_type=kinds[file.suffix])
  cookies={c['name']:c['value'] for c in page.context.cookies()}
  lang=cookies.get('lang','EN')
  context=dict(t=lambda key,**params:t(key,lang,**params),lang=lang,identity=S(role='superadmin'),page='history',csrf_token='fixture')
  sidebar=env.get_template('shared/sidebar.html').render(**context)
  assets=env.get_template('shared/calendar-assets.html').render(loading=False)
  html='<html lang="'+('en' if lang=='EN' else 'zh-CN')+'"><head><meta charset="utf-8"><link rel="stylesheet" href="/static/i18n.css"><link rel="stylesheet" href="/static/admin.css"><script src="/static/i18n.js"></script>'+assets+'</head><body>'+sidebar+'<main class="content"><label>Date<input id="date" type="date" value="2026-10-08"></label><label>Time<input id="time" type="datetime-local" step="1" value="2026-10-08T12:34:56"></label><label>Month<input id="month" type="month" value="2026-10"></label></main></body></html>'
  r.fulfill(body=html,content_type='text/html')
 page.route('https://gateway.test/**',route)
 page.goto('https://gateway.test/',wait_until='networkidle')
 for lang in ['EN','CN']:
  if lang=='CN':
   page.get_by_role('radio',name='中文',exact=True).check()
   page.wait_for_function("document.documentElement.lang==='zh-CN'")
  page.wait_for_function("document.querySelector('#month')._flatpickr")
  for width,height in [(1920,1080),(1366,768),(1280,720),(1024,650)]:
   page.set_viewport_size({'width':width,'height':height})
   result=page.locator('.sidebar nav').evaluate('(n)=>({client:n.clientHeight,scroll:n.scrollHeight})')
   assert result['scroll']<=result['client'],(lang,width,height,result)
  page.set_viewport_size({'width':1366,'height':768})
  for ident,value in [('date','2026-10-08'),('time','2026-10-08T12:34:56'),('month','2026-10')]:
   field=page.locator('#'+ident);field.click()
   cal=page.locator('.flatpickr-calendar.open')
   cal.wait_for()
   assert cal.get_by_role('button',name='清除' if lang=='CN' else 'Clear',exact=True).is_visible()
   assert field.input_value()==value
   if ident!='month':
    assert cal.locator('.flatpickr-weekday').first.inner_text().strip()==('一' if lang=='CN' else 'Sun')
   else:
    assert cal.locator('.flatpickr-monthSelect-month').first.inner_text()==('一月' if lang=='CN' else 'January')
   if ident=='date': page.screenshot(path='/tmp/calendar-sidebar-'+lang+'.png')
   cal.get_by_role('button',name='关闭' if lang=='CN' else 'Close',exact=True).click()
  page.reload(wait_until='networkidle')
  assert page.locator('[data-language-switch]:checked').input_value()==lang
 assert not errors,errors
 print('PASS: CN/EN calendars, ISO values, language switch and persistence; full navigation at 1080/768/720/650px heights')
 browser.close()

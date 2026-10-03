/* Shared browser rendering for JSON pages. Templates contain no runtime data. */
(() => {
  let environment;
  async function json(url, signal) {
    const response = await fetch(url, {signal, cache:'no-store', headers:{Accept:'application/json'}});
    if (response.redirected || !response.headers.get('content-type')?.includes('application/json'))
      throw new Error('登录状态已失效，请刷新页面重新登录');
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '加载失败，请重试');
    return data;
  }
  async function templates() {
    if (!environment) environment = (async () => {
      const sources = await json('/static/page-templates.json');
      nunjucks.installJinjaCompat();
      const Loader = nunjucks.Loader.extend({getSource(name) {
        if (!(name in sources)) throw new Error('未知页面模板');
        return {src:sources[name], path:name, noCache:false};
      }});
      const env = new nunjucks.Environment(new Loader(), {autoescape:true});
      env.addFilter('money', v => v == null ? '' : Number(v).toFixed(4));
      env.addFilter('tokens', v => v == null ? '' : window.TokenFormat.tokens(v));
      env.addFilter('attr', (obj, field) => obj?.[field]);
      env.addFilter('ownedby', (rows, owner) => rows.filter(r => r.owner_username===owner));
      env.addFilter('enabled', rows => rows.filter(r => r.enabled));
      env.addFilter('matching', (rows, field, value) => rows.filter(r => r[field]===value));
      env.addFilter('concat', (a,b) => a.concat(b));
      env.addFilter('tojson', value => JSON.stringify(value));
      return env;
    })().catch(error => {environment=undefined; throw error;});
    return environment;
  }
  function context(data) {
    const params = new URLSearchParams(location.search);
    return {...data, request:{
      query_params:{get:(key, fallback='') => params.get(key) ?? fallback},
      state:{user:data.identity},
      url:{path:location.pathname, include_query_params: args => {
        const url = new URL(location.href);
        for (const [key,value] of Object.entries(args)) if(key!=='__keywords') url.searchParams.set(key,value);
        return url.pathname+url.search;
      }},
      base_url:location.origin+'/'
    }};
  }
  async function render(name, data) {return (await templates()).render(name, context(data));}
  async function detail(url, signal) {
    const target=new URL(url, location.href); target.pathname+='/data';
    const data=await json(target,signal);
    const html=await render('shared/request-detail.html',data);
    const node=document.createElement('section'); node.innerHTML=html;
    return node.firstElementChild;
  }
  function pager(meta, label) {
    const nav=document.createElement('nav');nav.className='pagination';nav.setAttribute('aria-label',label);
    for(const [text,page] of [['上一页',meta.page-1],['下一页',meta.page+1]]) {
      const link=document.createElement('a');link.className='button button-small';link.textContent=text;
      const url=new URL(location.href);url.searchParams.set(meta.parameter,page);
      if(page<1 || page>meta.pages) {link.setAttribute('aria-disabled','true');link.removeAttribute('href');}
      else link.href=url.pathname+url.search;
      nav.append(link);
      if(text==='上一页') {const info=document.createElement('span');info.textContent='共 '+meta.total+' 条，第 '+meta.page+' / '+meta.pages+' 页';nav.append(info);}
    }
    return nav;
  }
  function initPickers() {
    document.querySelectorAll('select[name="username"],select[name="pinned_worker_id"]').forEach(select=>{
      const kind=select.name==='username'?'users':'workers';
      const controls=document.createElement('div');controls.className='option-picker';
      const search=document.createElement('input');search.type='search';search.placeholder=kind==='users'?'搜索用户':'搜索 Worker';search.setAttribute('aria-label',search.placeholder);
      const more=document.createElement('button');more.type='button';more.className='button button-small';more.textContent='更多选项';
      const status=document.createElement('span');status.setAttribute('role','status');
      controls.append(search,more,status);select.after(controls);
      let next=1, timer, controller;
      async function load(reset=false) {
        controller?.abort();controller=new AbortController();const signal=controller.signal;
        if(reset) next=1; more.disabled=true;
        try {
          const data=await json('/admin/options/'+kind+'?'+new URLSearchParams({search:search.value,page:next}),signal);
          if(signal.aborted)return;
          if(reset) {
            const selected=[...select.options].find(o=>o.selected);
            const blank=[...select.options].find(o=>o.value==='');
            select.replaceChildren();
            if(blank)select.append(blank);
            if(selected && selected!==blank)select.append(selected);
          }
          const existing=new Set([...select.options].map(o=>o.value));
          for(const item of data.options) if(!existing.has(item.value)){
            const option=new Option(item.label,item.value);option.disabled=!item.enabled;select.append(option);
          }
          next=data.pagination.page+1;more.hidden=next>data.pagination.pages;
          status.textContent='共 '+data.pagination.total+' 个选项';
        } catch(e){if(e.name!=='AbortError'){status.textContent=e.message;more.textContent='重试';more.hidden=false;}}
        finally{if(!signal.aborted)more.disabled=false;}
      }
      search.addEventListener('input',()=>{clearTimeout(timer);controller?.abort();timer=setTimeout(()=>load(true),250);});
      more.addEventListener('click',()=>load());
      // Select values used by edit dialogs remain available until explicitly changed.
      select.addEventListener('focus',()=>{if(next===1)load();},{once:true});
    });
  }
  let loading=false;
  async function load() {
    if(loading) return; loading=true;
    const body=document.body, main=document.querySelector('main.content'), error=main.querySelector('[data-page-error]'), retry=main.querySelector('[data-page-retry]');
    error.hidden=true;retry.hidden=true;main.setAttribute('aria-busy','true');
    try {
      const url=new URL(location.href);url.pathname+='/data';
      const data=await json(url);
      const html=await render(body.dataset.pageTemplate,data);
      const parsed=new DOMParser().parseFromString(html,'text/html');
      const content=parsed.querySelector('main.content');
      if(!content) throw new Error('页面布局加载失败');
      for(const link of parsed.querySelectorAll('link[rel=stylesheet]')) {
        if(!document.querySelector('link[href="'+link.getAttribute('href')+'"]')) document.head.append(document.importNode(link,true));
      }
      body.dataset.csrfToken=data.csrf_token;
      main.replaceWith(document.importNode(content,true));
      for(const dialog of parsed.querySelectorAll('body > dialog')) body.append(document.importNode(dialog,true));
      if(data.pagination) document.querySelector('main.content > section.panel')?.append(pager(data.pagination,'列表分页'));
      if(data.user_pagination) {
        const panels=document.querySelectorAll('main.content > section.panel');
        panels[1]?.append(pager(data.user_pagination,'用户汇总分页'));
        panels[2]?.append(pager(data.worker_pagination,'Worker 汇总分页'));
      }
      if(data.subscription_pagination) {
        const panel=[...document.querySelectorAll('main.content > section.panel')].find(p=>p.querySelector('[data-subscription-total]'));
        panel?.append(pager(data.subscription_pagination,'订阅套餐分页'));
      }
      if(data.page==='keys' || data.page==='admin_workers') initPickers();
      if(data.history) window.initialPageHistory=data.history;
      document.title=parsed.title;
      const loaded=new Set();
      for(const item of parsed.querySelectorAll('script[src]')) {
        const src=item.getAttribute('src');
        if(!src.startsWith('/static/') || src.includes('token-format.js') || src.includes('page-renderer.js') || src.includes('nunjucks-') || loaded.has(src)) continue;
        loaded.add(src);
        await new Promise((resolve,reject) => {
          const script=document.createElement('script');script.src=src;script.onload=resolve;script.onerror=()=>reject(new Error('交互脚本加载失败，请刷新页面'));document.head.append(script);
        });
      }
    } catch(e) {
      const target=document.querySelector('main.content');
      let message=target.querySelector('[data-page-error]');
      if(!message) {message=document.createElement('p');message.dataset.pageError='';message.setAttribute('role','alert');target.prepend(message);}
      message.textContent=e.message;message.hidden=false;
      if(retry.isConnected) retry.hidden=false;
      else {const button=document.createElement('button');button.className='button';button.textContent='刷新重试';button.onclick=()=>location.reload();target.append(button);}
    } finally {loading=false;document.querySelector('main.content').removeAttribute('aria-busy');}
  }
  window.PageRenderer={json,render,detail};
  if(document.body.dataset.jsonPage) {
    document.querySelector('[data-page-retry]').addEventListener('click',load);
    load();
  }
})();

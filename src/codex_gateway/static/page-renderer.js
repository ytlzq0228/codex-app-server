/* Shared browser rendering for JSON pages. Templates contain no runtime data. */
(() => {
  let environment;
  async function json(url, signal) {
    await globalThis.I18n.ready;
    const response = await fetch(url, {signal, cache:'no-store', headers:{Accept:'application/json'}});
    if (response.redirected || !response.headers.get('content-type')?.includes('application/json'))
      throw new Error(globalThis.I18n.t("登录状态已失效，请刷新页面重新登录"));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || (typeof data.detail === 'string' ? data.detail : globalThis.I18n.t("加载失败，请重试")));
    return data;
  }
  async function templates() {
    await globalThis.I18n.ready;
    if (!environment) environment = (async () => {
      const sources = await json('/static/page-templates.json');
      nunjucks.installJinjaCompat();
      const Loader = nunjucks.Loader.extend({getSource(name) {
        if (!(name in sources)) throw new Error(globalThis.I18n.t("未知页面模板"));
        return {src:sources[name], path:name, noCache:false};
      }});
      const env = new nunjucks.Environment(new Loader(), {autoescape:true});
      env.addGlobal('t', (name, params) => globalThis.I18n.t(name, undefined, params));
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
    return {...data, lang:globalThis.I18n.lang, html_lang:globalThis.I18n.locale, request:{
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
    for(const [text,page] of [[globalThis.I18n.t("上一页"),meta.page-1],[globalThis.I18n.t("下一页"),meta.page+1]]) {
      const link=document.createElement('a');link.className='button button-small';link.textContent=text;
      const url=new URL(location.href);url.searchParams.set(meta.parameter,page);
      if(page<1 || page>meta.pages) {link.setAttribute('aria-disabled','true');link.removeAttribute('href');}
      else link.href=url.pathname+url.search;
      nav.append(link);
      if(text===globalThis.I18n.t("上一页")) {const info=document.createElement('span');info.textContent=globalThis.I18n.t('共 {total} 条，第 {page} / {pages} 页', undefined, {total:meta.total, page:meta.page, pages:meta.pages});nav.append(info);}
    }
    return nav;
  }
  // One queue per page: deduplicate repeated users across rows/dialogs/pickers,
  // and drain every batch instead of sharing a deadline for the whole page.
  const displayNameValues=new Map(), displayNamePending=new Map(), displayNameQueue=[];
  let resolvingDisplayNames=false;
  async function drainDisplayNames() {
    if(resolvingDisplayNames)return;
    resolvingDisplayNames=true;
    try {
      while(displayNameQueue.length) {
        const batch=displayNameQueue.splice(0,5);
        let values={};
        try {
          const params=new URLSearchParams();
          batch.forEach(item=>params.append('usernames',item.name));
          const data=await json('/user/display-names?'+params);
          values=data.display_names || {};
        } catch(error) { /* Keep the rendered fallback; other batches still run. */ }
        for(const {name,resolve} of batch) {
          const value=typeof values[name]==='string'?values[name]:null;
          if(value!==null)displayNameValues.set(name,value);
          displayNamePending.delete(name);
          resolve(value);
        }
      }
    } finally {resolvingDisplayNames=false;}
  }
  function resolveDisplayName(name) {
    if(displayNameValues.has(name))return Promise.resolve(displayNameValues.get(name));
    if(displayNamePending.has(name))return displayNamePending.get(name);
    const result=new Promise(resolve=>displayNameQueue.push({name,resolve}));
    displayNamePending.set(name,result);
    // Gather all names discovered in this DOM pass before starting the batch.
    queueMicrotask(drainDisplayNames);
    return result;
  }
  async function hydrateDisplayNames(root=document) {
    await Promise.all([...root.querySelectorAll('[data-display-name]')].map(async node=>{
      const name=node.dataset.displayName;
      if(!name)return;
      const value=await resolveDisplayName(name);
      if(value===null || !node.isConnected || node.dataset.displayName!==name)return;
      node.textContent=value+(node.dataset.displayNameSuffix || '');
      // Do not fire change: a new label must not change or submit the selection.
      if(node.tagName==='OPTION')node.closest('select')?.dispatchEvent(new Event('display-names-updated'));
    }));
  }

  let ownerPickerId=0;
  function initOwnerPicker(select) {
    const t=name=>globalThis.I18n.t(name);
    const id='owner-picker-'+(++ownerPickerId);
    const trigger=document.createElement('button');
    trigger.type='button';trigger.className='owner-picker-trigger';
    trigger.setAttribute('aria-label',select.getAttribute('aria-label'));
    trigger.setAttribute('aria-haspopup','listbox');trigger.setAttribute('aria-expanded','false');
    const popup=document.createElement('div');popup.className='owner-picker-menu';popup.popover='auto';popup.id=id;
    const search=document.createElement('input');search.type='search';search.placeholder=t('搜索用户');
    search.setAttribute('aria-label',t('搜索用户'));search.setAttribute('role','combobox');
    search.setAttribute('aria-autocomplete','list');search.setAttribute('aria-controls',id+'-list');
    const list=document.createElement('div');list.className='owner-picker-options';list.id=id+'-list';list.setAttribute('role','listbox');list.setAttribute('aria-label',t('归属用户'));
    trigger.setAttribute('aria-controls',list.id);
    const footer=document.createElement('div');footer.className='owner-picker-footer';
    const status=document.createElement('span');status.setAttribute('role','status');
    const more=document.createElement('button');more.type='button';more.className='button button-small';more.textContent=t('更多选项');
    footer.append(status,more);popup.append(search,list,footer);select.after(trigger,popup);select.hidden=true;
    let rows=[],next=1,timer,controller,active=-1;
    const sync=()=>{trigger.textContent=select.selectedOptions[0]?.textContent || '—';};
    const opened=()=>popup.matches(':popover-open');
    const close=()=>{if(opened())popup.hidePopover();};
    const position=()=>{
      const box=trigger.getBoundingClientRect(),width=Math.min(Math.max(box.width,260),innerWidth-24);
      popup.style.width=width+'px';popup.style.left=Math.max(12,Math.min(box.left,innerWidth-width-12))+'px';
      const below=innerHeight-box.bottom-12,above=box.top-12;
      const down=below>=Math.min(320,above),space=down?below:above;
      popup.style.maxHeight=Math.max(80,space-6)+'px';
      popup.style.top=down?box.bottom+6+'px':'auto';popup.style.bottom=down?'auto':innerHeight-box.top+6+'px';
    };
    const activate=index=>{
      const options=[...list.children];active=index;
      options.forEach((node,i)=>node.classList.toggle('is-active',i===active));
      if(options[active]){search.setAttribute('aria-activedescendant',options[active].id);options[active].scrollIntoView({block:'nearest'});}
      else search.removeAttribute('aria-activedescendant');
    };
    const render=()=>{
      list.replaceChildren();active=-1;search.removeAttribute('aria-activedescendant');
      rows.forEach((item,i)=>{
        const option=document.createElement('button');option.type='button';option.className='owner-picker-option';option.id=id+'-option-'+i;
        option.setAttribute('role','option');option.setAttribute('aria-selected',String(item.value===select.value));
        option.tabIndex=-1;option.disabled=!item.enabled;option.textContent=item.label;option.title=item.value;
        option.dataset.displayName=item.value;
        option.addEventListener('click',()=>{
          let native=[...select.options].find(o=>o.value===item.value);
          if(!native){native=new Option(option.textContent,item.value);select.append(native);}
          native.dataset.displayName=item.value;hydrateDisplayNames(select);
          select.value=item.value;select.dispatchEvent(new Event('change',{bubbles:true}));sync();close();trigger.focus();
        });list.append(option);
      });
      hydrateDisplayNames(list);
    };
    async function load(reset=false) {
      controller?.abort();controller=new AbortController();const signal=controller.signal;
      if(reset){next=1;rows=[];render();}
      more.disabled=true;status.textContent=t('正在搜索…');
      try {
        const data=await json('/admin/options/users?'+new URLSearchParams({search:search.value,page:next}),signal);
        if(signal.aborted)return;
        const existing=new Set(rows.map(r=>r.value));rows.push(...data.options.filter(r=>!existing.has(r.value)));render();
        next=data.pagination.page+1;more.hidden=next>data.pagination.pages;more.textContent=t('更多选项');
        status.textContent=data.pagination.total?globalThis.I18n.t('共 {total} 个选项',undefined,{total:data.pagination.total}):t('没有匹配的用户');
      } catch(error) {if(!signal.aborted){status.textContent=error.message;more.textContent=t('重试');more.hidden=false;}}
      finally {if(!signal.aborted)more.disabled=false;}
    }
    popup.addEventListener('toggle',event=>{
      const open=event.newState==='open';trigger.setAttribute('aria-expanded',String(open));search.setAttribute('aria-expanded',String(open));
      if(open){position();search.focus();window.addEventListener('resize',position);}
      else {clearTimeout(timer);controller?.abort();window.removeEventListener('resize',position);}
    });
    const open=()=>{search.value='';load(true);popup.showPopover();position();};
    trigger.addEventListener('click',()=>{if(opened())close();else open();});
    select.addEventListener('owner-picker-open',()=>{sync();open();});
    select.addEventListener('change',sync);
    select.addEventListener('display-names-updated',sync);
    select.form.addEventListener('reset',()=>{close();setTimeout(sync,0);});
    search.addEventListener('input',()=>{clearTimeout(timer);controller?.abort();rows=[];render();status.textContent=t('正在搜索…');more.hidden=true;timer=setTimeout(()=>load(true),250);});
    more.addEventListener('click',()=>load());
    popup.addEventListener('keydown',event=>{
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();trigger.focus();}
      if(event.target!==search)return;
      if(event.key==='Enter'){event.preventDefault();if(active>=0)list.children[active]?.click();}
      if(['ArrowDown','ArrowUp'].includes(event.key)){
        event.preventDefault();const options=[...list.children],direction=event.key==='ArrowDown'?1:-1;
        let index=active<0?(direction===1?-1:options.length):active;
        do {index+=direction;} while(options[index]?.disabled);
        if(options[index])activate(index);
      }
    });
    sync();
  }
  function initPickers() {
    document.querySelectorAll('select[name="username"],select[name="pinned_worker_id"]').forEach(select=>{
      if(select.closest('[data-key-owner]')){initOwnerPicker(select);return;}
      const kind=select.name==='username'?'users':'workers';
      const controls=document.createElement('div');controls.className='option-picker';
      const search=document.createElement('input');search.type='search';search.placeholder=kind==='users'?globalThis.I18n.t("搜索用户"):globalThis.I18n.t("搜索 Worker");search.setAttribute('aria-label',search.placeholder);
      const more=document.createElement('button');more.type='button';more.className='button button-small';more.textContent=globalThis.I18n.t("更多选项");
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
            const option=new Option(item.label,item.value);option.disabled=!item.enabled;
            if(kind==='users')option.dataset.displayName=item.value;
            select.append(option);
          }
          if(kind==='users')hydrateDisplayNames(select);
          next=data.pagination.page+1;more.hidden=next>data.pagination.pages;
          status.textContent=globalThis.I18n.t('共 {total} 个选项', undefined, {total:data.pagination.total});
        } catch(e){if(e.name!=='AbortError'){status.textContent=e.message;more.textContent=globalThis.I18n.t("重试");more.hidden=false;}}
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
      const [data]=await Promise.all([json(url), templates()]);
      const html=await render(body.dataset.pageTemplate,data);
      const parsed=new DOMParser().parseFromString(html,'text/html');
      const content=parsed.querySelector('main.content');
      if(!content) throw new Error(globalThis.I18n.t("页面布局加载失败"));
      for(const link of parsed.querySelectorAll('link[rel=stylesheet]')) {
        if(!document.querySelector('link[href="'+link.getAttribute('href')+'"]')) document.head.append(document.importNode(link,true));
      }
      body.dataset.csrfToken=data.csrf_token;
      for(const dialog of document.querySelectorAll('body > dialog')) dialog.remove();
      main.replaceWith(document.importNode(content,true));
      for(const dialog of parsed.querySelectorAll('body > dialog')) body.append(document.importNode(dialog,true));
      if(data.pagination) document.querySelector('main.content > section.panel')?.append(pager(data.pagination,globalThis.I18n.t("列表分页")));
      if(data.user_pagination) {
        const panels=document.querySelectorAll('main.content > section.panel');
        panels[1]?.append(pager(data.user_pagination,globalThis.I18n.t("用户汇总分页")));
        panels[2]?.append(pager(data.worker_pagination,globalThis.I18n.t("Worker 汇总分页")));
      }
      if(data.subscription_pagination) {
        const panel=[...document.querySelectorAll('main.content > section.panel')].find(p=>p.querySelector('[data-subscription-total]'));
        panel?.append(pager(data.subscription_pagination,globalThis.I18n.t("订阅套餐分页")));
      }
      if(data.page==='keys' || data.page==='admin_workers') initPickers();
      hydrateDisplayNames();
      if(data.history) window.initialPageHistory=data.history;
      document.title=parsed.title;
      const loaded=new Set();
      for(const item of parsed.querySelectorAll('script[src]')) {
        const src=item.getAttribute('src');
        if(!src.startsWith('/static/') || src.includes('i18n.js') || src.includes('token-format.js') || src.includes('page-renderer.js') || src.includes('nunjucks-') || loaded.has(src)) continue;
        loaded.add(src);
        await new Promise((resolve,reject) => {
          const script=document.createElement('script');script.src=src;script.onload=resolve;script.onerror=()=>reject(new Error(globalThis.I18n.t("交互脚本加载失败，请刷新页面")));document.head.append(script);
        });
      }
    } catch(e) {
      const target=document.querySelector('main.content');
      let message=target.querySelector('[data-page-error]');
      if(!message) {message=document.createElement('p');message.dataset.pageError='';message.setAttribute('role','alert');target.prepend(message);}
      message.textContent=e.message;message.hidden=false;
      if(retry.isConnected) retry.hidden=false;
      else {const button=document.createElement('button');button.className='button';button.textContent=globalThis.I18n.t("刷新重试");button.onclick=()=>location.reload();target.append(button);}
    } finally {loading=false;document.querySelector('main.content').removeAttribute('aria-busy');}
  }
  window.PageRenderer={json,render,detail,hydrateDisplayNames};
  if(document.body.dataset.jsonPage) {
    document.querySelector('[data-page-retry]').addEventListener('click',load);
    load();
  }
})();

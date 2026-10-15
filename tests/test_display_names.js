const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const calls=[];
let inFlight=0,maxInFlight=0;
const nodes=[];
const document={body:{dataset:{}},querySelectorAll:()=>nodes};
const context={document,window:{},queueMicrotask,URLSearchParams,Event,
  I18n:{ready:Promise.resolve(),t:x=>x},fetch:async url=>{
    const names=new URLSearchParams(url.split('?')[1]).getAll('usernames');
    calls.push(names);inFlight++;maxInFlight=Math.max(maxInFlight,inFlight);
    await new Promise(resolve=>setTimeout(resolve,2));inFlight--;
    if(names.includes('user10'))throw new Error('temporary failure');
    return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({
      display_names:Object.fromEntries(names.map(name=>[name,'Display <'+name+'>']))})};
  }};
vm.runInNewContext(fs.readFileSync('src/codex_gateway/static/page-renderer.js','utf8'),context);
const hydrate=context.window.PageRenderer.hydrateDisplayNames;
function node(name,extra={}) {
  return {dataset:{displayName:name},isConnected:true,textContent:name,tagName:'SPAN',...extra};
}
(async()=>{
  for(let i=0;i<37;i++)nodes.push(node('user'+i));
  nodes.push(node('user0')); // Duplicate in an edit dialog.
  const select={value:'user36',events:[],dispatchEvent(event){this.events.push(event.type);}};
  const option=node('user36',{tagName:'OPTION',dataset:{displayName:'user36',displayNameSuffix:' · Admin'},closest:()=>select});
  nodes.push(option);
  const detached=node('detached',{isConnected:false});nodes.push(detached);
  await hydrate();
  assert.equal(maxInFlight,1);
  assert.equal(calls.length,8);
  assert(calls.every(batch=>batch.length<=5));
  assert.equal(calls.flat().filter(name=>name==='user0').length,1);
  assert.equal(nodes[0].textContent,'Display <user0>'); // Assigned as text, never HTML.
  assert.equal(nodes[10].textContent,'user10'); // Failed batch keeps fallback.
  assert.equal(nodes[36].textContent,'Display <user36>'); // Later batches still run.
  assert.equal(option.textContent,'Display <user36> · Admin');
  assert.equal(select.value,'user36');
  assert.deepEqual(select.events,['display-names-updated']);
  assert.equal(detached.textContent,'detached');
  const dynamic=node('user36');
  await hydrate({querySelectorAll:()=>[dynamic]});
  assert.equal(dynamic.textContent,'Display <user36>');
  assert.equal(calls.length,8); // A newly loaded option reuses an existing name.
  console.log('Display name batching, deduplication, fallback and DOM updates: passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

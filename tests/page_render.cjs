const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=path.resolve(__dirname,'../src/codex_gateway');
const payload=JSON.parse(fs.readFileSync(0,'utf8'));
global.window={};global.location=new URL(payload.url);
global.document={body:{dataset:{}}};
global.nunjucks=require(root+'/static/vendor/nunjucks-3.2.4.min.js');
global.fetch=async()=>({ok:true,redirected:false,headers:{get:()=> 'application/json'},json:async()=>JSON.parse(fs.readFileSync(root+'/static/page-templates.json','utf8'))});
vm.runInThisContext(fs.readFileSync(root+'/static/token-format.js','utf8'));
vm.runInThisContext(fs.readFileSync(root+'/static/page-renderer.js','utf8'));
(async()=>{
 const results=[];
 for(const data of payload.pages){
   const template=['overview','keys','admin_workers','sessions'].includes(data.page)?'admin/dashboard.html':'account.html';
   results.push(await window.PageRenderer.render(template,data));
 }
 process.stdout.write(JSON.stringify(results));
})().catch(e=>{console.error(e);process.exit(1)});

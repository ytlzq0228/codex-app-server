"""Verify a deployed release against its source manifest and live containers."""
import json,subprocess,pathlib,sys,hashlib,urllib.request,urllib.error
root=pathlib.Path(sys.argv[1]); manifest=json.loads((root/'release-manifest.json').read_text()); files=manifest['files']; tag='release-'+manifest['commit'][:7]
for name,digest in files.items(): assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest,name
print('DISK_MATCH',manifest['commit'],len(files),'files')
def out(*args): return subprocess.check_output(args,text=True).strip()
expected={k.removeprefix('src/codex_gateway/'):v for k,v in files.items() if k.startswith('src/codex_gateway/')}
program='''import pathlib,hashlib,json,codex_gateway
root=pathlib.Path(codex_gateway.__file__).parent
print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}))'''
for image, expected_image in manifest.get('images', {}).items():
 actual_image=json.loads(out('docker','image','inspect',image))[0]
 assert actual_image['RootFS']['Layers']==expected_image['layers'],image
 settings={k:v for k,v in actual_image['Config'].items() if v is not None}
 digest=hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()
 assert digest==expected_image['config_sha256'],image
print('IMAGE_LAYERS_AND_CONFIG_MATCH')
rows=[]
for name in ('codex-app-server-gateway-1','codex-app-server-worker-manager-1'):
 actual=json.loads(out('docker','exec',name,'python','-c',program)); assert actual==expected,(name,'runtime files mismatch',set(actual)^set(expected))
 info=json.loads(out('docker','inspect',name))[0]; assert info['Config']['Image']=='codex-gateway:'+tag
 assert info['Image']==out('docker','image','inspect','codex-gateway:'+tag,'--format','{{.Id}}')
 if name.endswith('worker-manager-1'):
  env=dict(item.split('=',1) for item in info['Config']['Env'])
  assert env['CODEX_WORKER_IMAGE']=='codex-gateway-worker:'+tag
  assert env['GEMINI_WORKER_IMAGE']=='codex-antigravity-worker:'+tag
 rows.append({'container':name,'image_id':info['Image'],'files_verified':len(actual)})
for name in out('docker','ps','--filter','label=io.codex-gateway.managed=true','--format','{{.Names}}').splitlines():
 info=json.loads(out('docker','inspect',name))[0]; gemini=info['Config'].get('Labels',{}).get('io.codex-gateway.provider')=='gemini'
 image=('codex-antigravity-worker' if gemini else 'codex-gateway-worker')+':'+tag
 assert info['Config']['Image']==image,(name,info['Config']['Image'])
 assert info['Image']==out('docker','image','inspect',image,'--format','{{.Id}}')
 pairs=[('worker/antigravity/service.py','/opt/service.py'),('worker/antigravity/client_bridge.py','/opt/client_bridge.py')] if gemini else [('worker/entrypoint.sh','/usr/local/bin/codex-worker')]
 for source,dest in pairs: assert out('docker','exec',name,'sha256sum',dest).split()[0]==files[source],name
 rows.append({'container':name,'image_id':info['Image'],'files_verified':len(pairs)})
with urllib.request.urlopen('http://127.0.0.1:8000/healthz') as r: assert r.status==200
with urllib.request.urlopen('http://127.0.0.1:8000/auth/login') as r: assert r.status==200 and r.headers['Referrer-Policy']=='same-origin'
for origin,fetch,expected_status in [('http://127.0.0.1:8000',None,400),('null','same-origin',400),('null',None,403),('https://evil.test','cross-site',403)]:
 headers={'Origin':origin,'X-Requested-With':'XMLHttpRequest'}
 if fetch: headers['Sec-Fetch-Site']=fetch
 req=urllib.request.Request('http://127.0.0.1:8000/auth/login',data=b'',headers=headers)
 try: response=urllib.request.urlopen(req)
 except urllib.error.HTTPError as e: response=e
 assert response.status==expected_status,(origin,fetch,response.status)
# Worker manager and Codex readiness checks run through the gateway's network.
code='''import urllib.request
assert urllib.request.urlopen("http://worker-manager:4600/healthz").status==200
assert urllib.request.urlopen("http://worker-1:4500/readyz").status==200
print("MANAGER_AND_CODEX_READY")'''
print(out('docker','exec','codex-app-server-gateway-1','python','-c',code))
print(json.dumps(rows,indent=2))
(root/'release-verification.json').write_text(json.dumps({'commit':manifest['commit'],'containers':rows,'status':'passed'},indent=2)+'\n')
print('VERIFIED',len(rows),'containers')

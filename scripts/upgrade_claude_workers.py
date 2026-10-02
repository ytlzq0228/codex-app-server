"""Run inside the gateway image with Docker socket and /release mounted read-only.
Recreate only the managed stable Claude Workers; retain ports, identity and volumes.
No running rollback copy is permitted. Original containers are removed on success.
"""
import json
from pathlib import Path
import time
import docker

client=docker.from_env()
manifest=json.loads(Path('/release/release-manifest.json').read_text())
image=manifest['component_images']['claude']
client.images.get(image)

account_code='''import os,json,urllib.request
r=urllib.request.Request('http://127.0.0.1:4500/account',data=b'{}',headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['CODEX_WORKER_TOKEN']})
with urllib.request.urlopen(r,timeout=35) as response: print(response.read().decode())
'''
busy_code='''from pathlib import Path
count=0
for p in Path('/proc').glob('[0-9]*/cmdline'):
 try:
  executable=p.read_bytes().split(b'\\0')[0].split(b'/')[-1]
  count+=executable==b'claude'
 except (FileNotFoundError,PermissionError,ProcessLookupError): pass
print(count)
'''

def account(container):
    result=container.exec_run(['/opt/runtime/bin/python','-c',account_code])
    if result.exit_code:
        raise RuntimeError('Account endpoint unavailable; no credential details logged')
    return json.loads(result.output).get('account')

for old in client.containers.list(filters={'label':'io.codex-gateway.managed=true'}):
    info=old.attrs
    if info['Config']['Image']!='codex-claude-worker:2.1.287':
        continue
    name=old.name
    previous=account(old)
    for attempt in range(20):
        result=old.exec_run(['/opt/runtime/bin/python','-c',busy_code])
        if result.exit_code==0 and result.output.strip()==b'0': break
        time.sleep(3)
    else:
        raise RuntimeError('Claude Worker still busy: '+name)
    config,host=info['Config'],info['HostConfig']
    volumes={}
    for mount in info['Mounts']:
        if mount['Type'] not in {'volume','bind'}: continue
        if mount['Destination'] in {'/opt/service.py','/opt'}:
            raise RuntimeError('Custom service mount must be reviewed: '+name)
        source=mount['Name'] if mount['Type']=='volume' else mount['Source']
        volumes[source]={'bind':mount['Destination'],'mode':'rw' if mount['RW'] else 'ro'}
    ports={key:[(binding.get('HostIp',''),int(binding['HostPort'])) for binding in bindings]
           for key,bindings in (info['NetworkSettings'].get('Ports') or {}).items() if bindings}
    network=next(iter(info['NetworkSettings']['Networks']))
    rollback=name+'-before-login-fix'
    try:
        existing=client.containers.get(rollback)
        if existing.status=='running': raise RuntimeError('Rollback copy is running')
        existing.remove()
    except docker.errors.NotFound: pass
    old.stop(timeout=20)
    old.rename(rollback)
    new=None
    try:
        new=client.containers.run(image,name=name,detach=True,command=config['Cmd'],entrypoint=config['Entrypoint'],
            user=config.get('User'),environment=config['Env'],labels=config.get('Labels') or {},
            network=network,volumes=volumes,ports=ports,read_only=host.get('ReadonlyRootfs',True),
            tmpfs=host.get('Tmpfs') or {},cap_drop=host.get('CapDrop') or ['ALL'],
            security_opt=host.get('SecurityOpt') or ['no-new-privileges:true'],
            pids_limit=host.get('PidsLimit',256),mem_limit=host.get('Memory',4294967296),
            nano_cpus=host.get('NanoCpus',2000000000),restart_policy=host.get('RestartPolicy') or {'Name':'unless-stopped'},
            working_dir=config.get('WorkingDir') or None,extra_hosts=host.get('ExtraHosts') or None)
        for attempt in range(30):
            try:
                current=account(new)
                # Compare identity without printing subscription or credential details.
                assert bool(current)==bool(previous), 'login state changed'
                if previous:
                    assert (current.get('email'),current.get('project'))==(previous.get('email'),previous.get('project')), 'account changed'
                break
            except Exception:
                if attempt==29: raise
                time.sleep(1)
        old.remove()
        print(json.dumps({'worker':name,'image':image,'account_preserved':True}))
    except BaseException:
        if new:
            new.remove(force=True)
        old.rename(name)
        old.start()
        raise

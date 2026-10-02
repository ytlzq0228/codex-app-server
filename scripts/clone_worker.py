import docker,json,sys
spec=json.load(sys.stdin); client=docker.from_env(); c=spec['inspect']['Config']; h=spec['inspect']['HostConfig']
if c.get('Labels', {}).get('io.codex-gateway.managed') != 'true':
    raise ValueError('Only gateway-managed Workers may be cloned')
volumes={m['Name']:{'bind':m['Destination'],'mode':'rw' if m['RW'] else 'ro'} for m in spec['inspect']['Mounts'] if m['Type']=='volume'}
container=client.containers.run(c['Image'],name=spec['name'],detach=True,
    command=c['Cmd'],entrypoint=c['Entrypoint'],user=c.get('User'),environment=c['Env'],
    labels={k:v for k,v in c.get('Labels',{}).items() if not k.startswith('com.docker.compose.')},
    network=spec['network'],volumes=volumes,ports={'4500/tcp':(spec['ip'],None)},
    read_only=h.get('ReadonlyRootfs',True),tmpfs=h.get('Tmpfs') or {},
    cap_drop=h.get('CapDrop') or ['ALL'],security_opt=h.get('SecurityOpt') or ['no-new-privileges:true'],
    pids_limit=h.get('PidsLimit',256),mem_limit=h.get('Memory',4294967296),
    nano_cpus=h.get('NanoCpus',2000000000),restart_policy={'Name':'unless-stopped'},
    working_dir=c.get('WorkingDir') or None)
container.reload();port=container.attrs['NetworkSettings']['Ports']['4500/tcp'][0]['HostPort']
print(json.dumps({'name':container.name,'port':port}))

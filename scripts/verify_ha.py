"""Read-only verification of a deployed dual-active node; run with Docker access."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

root = Path(sys.argv[1])
manifest = json.loads((root / 'release-manifest.json').read_text())
for name, digest in manifest['files'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name

def output(*command):
    return subprocess.check_output(command, text=True).strip()

expected = {name.removeprefix('src/codex_gateway/'): digest for name, digest in manifest['files'].items()
            if name.startswith('src/codex_gateway/')}
package_check = '''import pathlib,hashlib,json,codex_gateway
root=pathlib.Path(codex_gateway.__file__).parent
print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}))'''
for name in ('codex-app-server-gateway-1', 'codex-app-server-worker-manager-1'):
    actual = json.loads(output('docker', 'exec', name, 'python', '-c', package_check))
    assert actual == expected, (name, 'runtime source differs')
    info = json.loads(output('docker', 'inspect', name))[0]
    assert info['Image'] == output('docker', 'image', 'inspect', manifest['component_images']['gateway'], '--format', '{{.Id}}')

state_check = '''import asyncio,json
from sqlalchemy import select,text
from codex_gateway.config import get_settings
from codex_gateway.database import SessionLocal,engine
from codex_gateway.models import AppNode,Worker
async def run():
 s=get_settings()
 async with SessionLocal() as db:
  assert not await db.scalar(text('SELECT pg_is_in_recovery()'))
  nodes=(await db.scalars(select(AppNode))).all()
  workers=(await db.scalars(select(Worker).where(Worker.node_id==s.node_id,Worker.endpoint!='removed://worker'))).all()
  print(json.dumps({'node_id':s.node_id,'nodes':[n.id for n in nodes],'workers':[{'container':w.container_name,'provider':w.provider,'endpoint':w.endpoint,'status':w.status.value} for w in workers]}))
 await engine.dispose()
asyncio.run(run())'''
state = json.loads(output('docker', 'exec', 'codex-app-server-gateway-1', 'python', '-c', state_check))
active = output('docker', 'ps', '--filter', 'label=io.codex-gateway.managed=true', '--format', '{{.Names}}').splitlines()
assert set(active) == {worker['container'] for worker in state['workers']}, ('managed instances differ', active)
for worker in state['workers']:
    info = json.loads(output('docker', 'inspect', worker['container']))[0]
    expected_image = manifest['component_images'][worker['provider']]
    assert info['Image'] == output('docker', 'image', 'inspect', expected_image, '--format', '{{.Id}}'), worker['container']
    assert info['NetworkSettings']['Ports']['4500/tcp'], worker['container']
with urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5) as response:
    assert response.status == 200
result = {'release': manifest['release'], 'status': 'passed', **state}
(root / 'release-verification.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))

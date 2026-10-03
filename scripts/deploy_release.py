#!/usr/bin/env python3
"""Apply a tested gateway/manager image without recreating user Workers.
Run as root: deploy_release.py APP_DIRECTORY ARTIFACT_DIRECTORY
Artifacts: source.tar.gz, image.tar, release-manifest.json. Test deployments
back up first and keep only the latest backup. Validated production releases
in /opt/codex-app-server-ha deploy directly without creating a backup.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('application_directory', type=Path)
    parser.add_argument('artifact_directory', type=Path)
    args = parser.parse_args()
    root = args.application_directory.resolve(strict=True)
    artifacts = args.artifact_directory.resolve(strict=True)
    if os.geteuid() != 0 or not (root / 'compose.yaml').is_file():
        parser.error('run as root against an existing application directory')
    manifest = json.loads((artifacts / 'release-manifest.json').read_text())
    image = manifest['component_images']['gateway']
    gateway, manager = json.loads(subprocess.check_output(['docker', 'inspect', 'codex-app-server-gateway-1', 'codex-app-server-worker-manager-1']))
    with tempfile.TemporaryDirectory(prefix='codex-release-') as temporary:
        stage = Path(temporary)
        with tarfile.open(artifacts / 'source.tar.gz') as archive:
            archive.extractall(stage, filter='data')
        for name, digest in manifest['files'].items():
            if hashlib.sha256((stage / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('artifact hash mismatch: ' + name)
        subprocess.run(['docker', 'load', '-i', str(artifacts / 'image.tar')], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['docker', 'image', 'inspect', image], check=True, stdout=subprocess.DEVNULL)
        historical = Path('/opt/codex-app-server/deploy-backups')
        backup_cmd = ['python3', str(stage / 'scripts/backup_release.py'), str(root)]
        if root == Path('/opt/codex-app-server-ha') and historical.exists():
            backup_cmd += ['--historical-root', str(historical)]
        production = root == Path('/opt/codex-app-server-ha') and (root / '.env.ha').exists()
        if not production:
            subprocess.run(backup_cmd, check=True)
        else:
            print(json.dumps({'backup': 'skipped', 'reason': 'validated production release'}), flush=True)
        # Preserve environment-specific Compose files and private settings.
        for name in ('src', 'scripts', 'docs', 'deploy', 'worker'):
            if not (stage / name).exists():
                continue
            if (root / name).exists():
                shutil.rmtree(root / name)
            shutil.copytree(stage / name, root / name)
        for name in ('Dockerfile', 'pyproject.toml', 'README.md', '.dockerignore', '.env.example'):
            shutil.copy2(stage / name, root / name)
        if (root / '.env.ha').exists():
            dotenv = root / '.env'
            contents = dotenv.read_text()
            contents = re.sub(r'^GATEWAY_IMAGE=.*$', 'GATEWAY_IMAGE=' + image, contents, flags=re.M)
            if not re.search(r'^GATEWAY_IMAGE=', contents, re.M):
                raise RuntimeError('missing GATEWAY_IMAGE setting')
            dotenv.write_text(contents)
            manager_env = root / '.env.manager.ha'
            content = manager_env.read_text()
            pin = 'CLAUDE_WORKER_IMAGE=' + manifest['component_images']['claude']
            content = re.sub(r'^CLAUDE_WORKER_IMAGE=.*$', pin, content, flags=re.M) if re.search(r'^CLAUDE_WORKER_IMAGE=', content, re.M) else content.rstrip() + '\n' + pin + '\n'
            manager_env.write_text(content)
            compose = ['docker', 'compose', '--project-directory', str(root), '--env-file', str(dotenv), '-p', 'codex-ha', '-f', str(root / 'compose.yaml')]
        else:
            override = root / 'compose.gemini-test.json'
            config = json.loads(override.read_text())
            for name, container in (('gateway', gateway), ('worker-manager', manager)):
                service = config.setdefault('services', {}).setdefault(name, {})
                service['image'] = image
                service['build'] = {'context': '.'}
                service['environment'] = dict(item.split('=', 1) for item in container['Config']['Env'])
                if name == 'worker-manager':
                    service['environment']['CLAUDE_WORKER_IMAGE'] = manifest['component_images']['claude']
            override.write_text(json.dumps(config, indent=2) + '\n')
            os.chmod(override, 0o600)
            compose = ['docker', 'compose', '--project-directory', str(root), '-p', 'codex-app-server', '-f', str(root / 'compose.yaml'), '-f', str(override)]
        shutil.copy2(artifacts / 'release-manifest.json', root / 'release-manifest.json')
        subprocess.run(compose + ['up', '-d', '--no-build', '--no-deps', 'worker-manager', 'gateway'], check=True)
        for attempt in range(45):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3) as response:
                    if response.status == 200:
                        break
            except Exception:
                pass
            time.sleep(2)
        else:
            raise RuntimeError('gateway did not become healthy; inspect deployed services before continuing')
        print(json.dumps({'release': manifest['release'], 'directory': str(root), 'health': 'ok'}))


if __name__ == '__main__':
    main()

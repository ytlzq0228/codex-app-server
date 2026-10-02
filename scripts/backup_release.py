#!/usr/bin/env python3
"""Keep exactly one deployment backup. Run as root on the application host.

Historical backups are deleted BEFORE creating the new backup, as requested.
The service and all worker containers remain running. Does not restore DB data.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from urllib.parse import urlsplit, unquote


def output(*args):
    return subprocess.check_output(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('application_directory', type=Path)
    parser.add_argument('--historical-root', action='append', default=[], type=Path)
    args = parser.parse_args()
    root = args.application_directory.resolve(strict=True)
    if root == Path('/') or not (root / 'compose.yaml').is_file():
        parser.error('application directory must contain compose.yaml')
    if os.geteuid() != 0:
        parser.error('run as root; backups contain private configuration')
    gateway = json.loads(output('docker', 'inspect', 'codex-app-server-gateway-1'))[0]
    manager = json.loads(output('docker', 'inspect', 'codex-app-server-worker-manager-1'))[0]
    managed_ids = output('docker', 'ps', '-q', '--filter', 'label=io.codex-gateway.managed=true').decode().split()
    managed = json.loads(output('docker', 'inspect', *managed_ids)) if managed_ids else []
    claude = [container for container in managed if container['Config']['Image'].startswith('codex-claude-worker:')]
    env = dict(item.split('=', 1) for item in gateway['Config']['Env'])
    url = urlsplit(env['CODEX_GATEWAY_DATABASE_URL'])
    network = next(iter(gateway['NetworkSettings']['Networks']))
    db_env = {**os.environ, 'PGPASSWORD': unquote(url.password or '')}
    db_cmd = ['docker', 'run', '--rm', '--network', network, '--env', 'PGPASSWORD', 'postgres:16-alpine']
    db_args = ['-h', url.hostname, '-p', str(url.port or 5432), '-U', unquote(url.username or ''), '-d', unquote(url.path.lstrip('/'))]
    # Verify DB access, Docker image and config before removing old backups.
    subprocess.run(db_cmd + ['psql', *db_args, '-Atc', 'SELECT 1'], env=db_env, check=True, stdout=subprocess.DEVNULL)
    backup_root = root / 'deploy-backups'
    roots = [backup_root, *args.historical_root]
    for directory in roots:
        # Cleanup only explicit deploy-backups directories; never follow symlinks.
        if directory.name != 'deploy-backups' or directory.is_symlink() or directory.resolve() == root:
            parser.error('backup roots must be real deploy-backups directories')
    for directory in roots:
        if directory.exists():
            for child in directory.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    backup = backup_root / ('release-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    (backup / 'containers.json').write_text(json.dumps([gateway, manager, *claude], indent=2))
    with (backup / 'database.dump').open('wb') as dump:
        subprocess.run(db_cmd + ['pg_dump', *db_args, '-Fc'], env=db_env, check=True, stdout=dump)
    with tarfile.open(backup / 'application.tar.gz', 'w:gz') as archive:
        for child in root.iterdir():
            if child.name not in {'deploy-backups', '.git', '.venv', '__pycache__', '.pytest_cache'} and not child.name.endswith(('.tar', '.tar.gz')):
                archive.add(child, arcname=child.name)
    images = sorted({gateway['Image'], manager['Image'], *(container['Image'] for container in claude)})
    with (backup / 'application-images.tar').open('wb') as archive:
        subprocess.run(['docker', 'save', *images], check=True, stdout=archive)
    (backup / 'README.txt').write_text('Rollback: restore configuration and load saved gateway/manager and running Claude Worker images. Container settings include prior Claude ports and volumes. Account volumes remain at their original locations; stop the current Worker before restoring a prior container. Database dump is a consistent snapshot; reconcile later writes before restoring it.\n')
    print(json.dumps({'backup': str(backup), 'retention': 1, 'database_bytes': (backup / 'database.dump').stat().st_size}))


if __name__ == '__main__':
    main()

"""Release backups must work without database credentials or connectivity."""
import json
import runpy
import sys
from pathlib import Path


def test_application_backup_never_contacts_database(tmp_path, monkeypatch):
    import os
    import subprocess
    root = tmp_path / 'application'
    root.mkdir()
    (root / 'compose.yaml').write_text('services: {}\n')
    container = {'Image': 'sha256:old', 'Config': {'Image': 'gateway:old'}}
    calls = []

    def output(command):
        calls.append(command)
        if list(command[:2]) == ['docker', 'inspect']:
            return json.dumps([container]).encode()
        if list(command[:2]) == ['docker', 'ps']:
            return b''
        raise AssertionError(command)

    def run(command, **kwargs):
        calls.append(command)
        assert command[:2] == ['docker', 'save']
        kwargs['stdout'].write(b'image archive')

    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(subprocess, 'check_output', output)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(sys, 'argv', ['backup_release.py', str(root)])
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/backup_release.py'), run_name='__main__')
    backup = next((root / 'deploy-backups').iterdir())
    assert (backup / 'application.tar.gz').is_file()
    assert (backup / 'application-images.tar').is_file()
    assert not (backup / 'database.dump').exists()
    assert all('pg_dump' not in command and 'psql' not in command for command in calls)

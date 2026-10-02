import importlib
from types import SimpleNamespace

import docker
import pytest


@pytest.mark.parametrize("provider,image,home,scheme", [
    ("codex", "codex-test", "/home/codex/.codex", "ws"),
    ("gemini", "codex-antigravity-worker:1.2.12", "/home/agy", "http"),
    ("claude", "codex-claude-worker:2.1.287", "/home/claude", "http"),
])
def test_manager_provider_volume_and_isolation(monkeypatch, provider, image, home, scheme):
    seen = {}
    def run(image, **kwargs):
        seen.update(image=image, **kwargs)
        return SimpleNamespace(id="test")
    client = SimpleNamespace(containers=SimpleNamespace(run=run))
    monkeypatch.setattr(docker, "from_env", lambda: client)
    manager = importlib.import_module("codex_gateway.manager")
    monkeypatch.setattr(manager, "client", client)
    for name, value in {"CODEX_MANAGER_TOKEN": "manager-test", "CODEX_WORKER_TOKEN": "worker-test",
                        "CODEX_WORKER_IMAGE": "codex-test", "CODEX_DOCKER_NETWORK": "test-network"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CLAUDE_WORKER_IMAGE", raising=False)
    monkeypatch.delenv("GEMINI_WORKER_IMAGE", raising=False)
    result = manager.create_worker(manager.WorkerSpec(name="worker-test", provider=provider), "Bearer manager-test")
    assert result["endpoint"] == scheme + "://worker-test:4500"
    assert seen["image"] == image
    assert seen["volumes"]["worker-test-" + provider + "-home"]["bind"] == home
    assert seen["read_only"] and seen["cap_drop"] == ["ALL"]
    assert seen["security_opt"] == ["no-new-privileges:true"]
    assert seen["environment"] == {"CODEX_WORKER_TOKEN": "worker-test"}

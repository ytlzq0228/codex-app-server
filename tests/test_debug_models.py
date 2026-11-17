from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from codex_gateway import reporting
from codex_gateway.config import get_settings
from codex_gateway.models import ModelMapping


@pytest.mark.asyncio
@pytest.mark.parametrize('auth_mode', ['chatgpt', 'apiKey'])
async def test_debug_models_apply_account_restrictions_and_include_mappings(monkeypatch, auth_mode):
    settings = get_settings()
    monkeypatch.setattr(settings, 'allowed_models', 'gpt-6.1-sol,gpt-6-sol,claude-test')
    monkeypatch.setattr(settings, 'model_aliases', '')
    monkeypatch.setattr(settings, 'model_auth_restrictions', {'gpt-6.1-sol': ['chatgpt']})
    worker = SimpleNamespace(provider='codex', auth_mode=auth_mode)
    mappings = {'custom-model': 'gpt-6-sol', 'restricted-alias': 'gpt-6.1-sol'}

    async def get(model, key):
        assert model is ModelMapping
        return SimpleNamespace(upstream_model=mappings[key]) if key in mappings else None

    db = SimpleNamespace(
        get=get,
        scalars=AsyncMock(side_effect=[list(mappings), ['codex'], SimpleNamespace(all=lambda: [worker])]),
        scalar=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(reporting, 'render', lambda request, identity, **context: context)
    context = await reporting.debug.__wrapped__(None, SimpleNamespace(username='tester'), db)
    ids = [model['id'] for model in context['debug_models']]
    assert 'gpt-6-sol' in ids
    assert 'custom-model' in ids
    assert 'claude-test' not in ids
    assert ('gpt-6.1-sol' in ids) == (auth_mode == 'apiKey')
    assert ('restricted-alias' in ids) == (auth_mode == 'apiKey')

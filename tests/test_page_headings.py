"""Check headings after rendering with the production browser template engine."""
import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("keys", [[], [{
    "id": "example-key", "name": "Example key", "prefix": "example",
    "enabled": True, "scheduling_mode": "pooled",
}]])
def test_api_keys_heading_after_data_load(keys):
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("page_render.cjs"))],
        input=json.dumps({
            "url": "http://testserver/admin/api-keys",
            "pages": [{"page": "keys", "keys": keys, "workers": [], "users": []}],
        }),
        capture_output=True, text=True, check=True,
    )
    html = json.loads(result.stdout)[0]
    header = html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert '<p class="eyebrow">ACCESS CONTROL</p>' in header
    assert "<h1>API Keys</h1>" in header
    assert '<p class="muted">创建、编辑、停用或删除 API Key。</p>' in header
    assert "系统在线" in header
    assert "[native code]" not in html

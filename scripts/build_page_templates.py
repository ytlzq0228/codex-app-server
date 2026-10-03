"""Generate browser templates from the shared Jinja layouts (no user data)."""
import json
import re
from pathlib import Path

def browser_tuples(source):
    # Jinja tuples are arrays in JSON/JavaScript; Nunjucks treats (a,b) as b.
    def convert(match):
        text = list(match.group(0))
        stack, quote, escaped = [], None, False
        for index, char in enumerate(text):
            if quote:
                if escaped:
                    escaped = False
                elif char == chr(92):
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
            elif char in "([{":
                prefix = "".join(text[:index]).rstrip()
                call = bool(prefix and (prefix[-1].isalnum() or prefix[-1] in "_)]"))
                stack.append([char, index, False, call])
            elif char == "," and stack:
                stack[-1][2] = True
            elif char in ")]}" and stack:
                opening, start, comma, call = stack.pop()
                if opening == "(" and comma and not call:
                    text[start], text[index] = "[", "]"
        return "".join(text)
    return re.sub(r"{{.*?}}|{%.*?%}", convert, source, flags=re.S)


root = Path(__file__).resolve().parents[1] / "src/codex_gateway"
templates = {}
for path in (root / "templates").rglob("*.html"):
    name = path.relative_to(root / "templates").as_posix()
    if name in {"login.html", "data-page.html", "shared/sidebar.html", "errors/403.html", "admin/infra.html", "shared/conversation-history.html"}:
        continue
    text = path.read_text()
    text = text.replace('{% include "shared/sidebar.html" %}', "")
    text = text.replace(".isoformat()", "").replace(".status.value", ".status")
    text = text.replace("user.password_hash", "user.has_password").replace("user.google_sub", "user.has_google")
    text = text.replace("config.client_secret", "config.has_secret")
    text = text.replace("|selectattr('owner_username','equalto',user.username)|list", "|ownedby(user.username)")
    text = text.replace("record.logical_conversation_id or '未关联'), ('后端 Thread', record.thread_id or '未记录')] + evidence_fields",
                        "record.logical_conversation_id or '未关联'), ('后端 Thread', record.thread_id or '未记录')]|concat(evidence_fields)")
    # Nunjucks supports tuples but filtered loops need an explicit filter.
    text = text.replace("for worker in workers if worker.enabled", "for worker in workers|enabled")
    text = text.replace("for worker in workers if worker.id == key.pinned_worker_id", "for worker in workers|matching('id', key.pinned_worker_id)")
    text = text.replace(".startswith(", ".startsWith(")
    text = text.replace("if not workers", "if not workers|length")
    text = text.replace("if attention", "if attention|length")
    text = text.replace("if owned else", "if owned|length else")
    text = text.replace("if observation_fields", "if observation_fields|length")
    templates[name] = browser_tuples(text)
(root / "static/page-templates.json").write_text(json.dumps(templates, ensure_ascii=False))

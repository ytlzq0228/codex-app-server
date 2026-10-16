"""Persist safe exception structure, never exception text, arguments or locals."""
import os
import traceback
from pathlib import PurePath
from .audit import current_audit

def capture_failure(exc):
    audit = current_audit.get()
    if audit is None:
        return
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 6:
        seen.add(id(exc))
        chain.append({"exception": type(exc).__name__,
                      "frames": [{"file": PurePath(f.filename).name, "line": f.lineno, "function": f.name}
                                 for f in traceback.extract_tb(exc.__traceback__)[-8:]]})
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    from .config import get_settings
    audit["diagnostic"] = {"exceptions": chain, "node": get_settings().node_id,
                           "release": os.environ.get("CODEX_GATEWAY_RELEASE", "unknown")[:120]}

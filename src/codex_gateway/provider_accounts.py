"""Subscription-provider account routing without changing Codex's RPC path."""


async def probe_provider(worker, db, settings, **kwargs):
    if worker.provider == "claude":
        from .claude_backend import probe_claude
        return await probe_claude(worker, db, settings, **kwargs)
    from .gemini_backend import probe_gemini
    return await probe_gemini(worker, db, settings, **kwargs)

# Codex hosted web search

The Responses endpoint accepts one top-level `{"type":"web_search"}` for Codex models. This enables native hosted search without enabling shell, filesystem writes, MCP servers or browser/computer tools.

- Default: live search. `external_web_access: false` selects cached search.
- Completed turns release their Worker thread subscription before returning; the pooled connection stays open. This lets the next resume apply new search settings. Pending client-tool turns keep their subscription.
- Omitted search or `tool_choice: "none"` disables search on new and resumed threads.
- Search, page-open and page-find actions return `web_search_call` items and SSE progress/completion events. Search needs no client tool output.
- Request `include: ["web_search_call.action.sources"]` to receive retrieved URLs.
- Answers preserve Markdown links. Structured URL annotations are added only to explicit links matching an observed source URL (fragments may differ). Opaque markers or unmatched URLs are not guessed.
- Search works alongside function/custom tools. While a client tool is pending, explicit declarations must retain the search configuration. Omitting declarations inherits the running turn.
- Full-history input accepts completed search items as historical context, never executable calls. Search options participate in checkpoint matching.
- Claude/Gemini, nested/additional search declarations, duplicates, forced search selection, max_tool_calls, and options other than external_web_access are explicitly unsupported.

Requires Codex app-server native webSearch events and web_search thread configuration (validated with 0.157.0). Upstream account/model restrictions still apply.

Validation: `tests/test_web_search.py` covers policy, events, sources, tool contracts and history. `scripts/validate_web_search.py` exercises real Responses JSON/SSE, previous_response_id mode changes, open/find, mixed client tools and history replay. Run against test with GATEWAY_URL and GATEWAY_API_KEY.

"""Codex hosted search, separate from client-executed dynamic tools."""
import re
from urllib.parse import urldefrag, urlsplit


def configuration(request):
    from .client_tools import ToolProtocolError
    from .providers import provider_for
    declarations = []
    for tool in request.tools or []:
        if tool.get('type') != 'web_search':
            continue
        if provider_for(request.model) != 'codex':
            raise ToolProtocolError('web_search is only supported for Codex Responses')
        unknown = set(tool) - {'type', 'external_web_access', 'search_content_types'}
        if unknown:
            raise ToolProtocolError('Unsupported web_search option: ' + sorted(unknown)[0])
        if 'external_web_access' in tool and not isinstance(tool['external_web_access'], bool):
            raise ToolProtocolError('web_search.external_web_access must be a boolean')
        content_types = tool.get('search_content_types', ['text'])
        if (not isinstance(content_types, list) or not content_types
                or any(value not in ('text', 'image') for value in content_types)):
            raise ToolProtocolError("web_search.search_content_types must be a non-empty list containing text or image")
        declarations.append(tool)
    if len(declarations) > 1:
        raise ToolProtocolError('Declare web_search at most once')
    if not declarations:
        return None
    if request.max_tool_calls is not None:
        raise ToolProtocolError('max_tool_calls is not supported with web_search')
    tool = declarations[0]
    content_types = tool.get('search_content_types', ['text'])
    config = {'mode': 'disabled' if request.tool_choice == 'none' or 'text' not in content_types else
              'live' if tool.get('external_web_access', True) else 'cached'}
    if 'image' in content_types:
        config['image_search_unavailable'] = True
    return config


def model_notice(request):
    config = configuration(request) or {}
    if not config.get('image_search_unavailable'):
        return None
    fallback = ('Text web search is available; use text queries to find relevant web pages instead.'
                if config['mode'] != 'disabled' else
                'Hosted web search is disabled for this request; use other available search tools if appropriate.')
    return ('Gateway capability notice: Image search is unavailable and has not been performed. '
            + fallback + ' Do not claim to have searched images. If no suitable tool is available, '
            'explain this limitation to the user rather than inventing search results.')


def mode(request):
    return (configuration(request) or {}).get('mode', 'disabled')


def native_item(item, completed):
    """Convert app-server search/open/find notifications to Responses items."""
    action = item.get('action') or {}
    kind = {'openPage': 'open_page', 'findInPage': 'find_in_page'}.get(action.get('type'), action.get('type'))
    public = {}
    if kind:
        public['type'] = kind
    for key in ('query', 'queries', 'url', 'pattern'):
        if action.get(key) is not None:
            public[key] = action[key]
    sources = [dict(url=r['url'], title=r.get('title') or r['url'])
               for r in item.get('results') or [] if isinstance(r, dict) and
               isinstance(r.get('url'), str) and urlsplit(r['url']).scheme in {'http', 'https'}]
    # Some app-server find events contain only a ref ID; the retrieved result
    # supplies the concrete URL required by the public action.
    if kind == 'find_in_page' and not public.get('url') and sources:
        public['url'] = sources[0]['url']
    if kind == 'search':
        public['sources'] = [{'type': 'url', 'url': r['url']} for r in sources]
    result = {'id': item['id'], 'type': 'web_search_call',
              'status': 'completed' if completed else 'in_progress'}
    if public:
        result['action'] = public
    return result, sources


def public_item(item, request):
    result = {**item}
    if 'action' in result:
        result['action'] = {**result['action']}
        if 'web_search_call.action.sources' not in (request.include or []):
            result['action'].pop('sources', None)
    return result


def annotations(text, sources):
    """Annotate explicit Markdown links only when backed by a retrieved URL.

    Never invent offsets or resolve an opaque citation marker by guessing.
    Unmatched links remain visible Markdown links.
    """
    known = {urldefrag(s['url'])[0]: s for s in sources}
    result = []
    for match in re.finditer(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)', text):
        source = known.get(urldefrag(match[2])[0])
        if source:
            result.append({'type': 'url_citation', 'url': match[2],
                           'title': source.get('title') or match[1],
                           'start_index': match.start(), 'end_index': match.end()})
    return result

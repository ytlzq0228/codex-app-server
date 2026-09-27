"""Private parser entry point. Reads JSON data, never imports or runs client code."""
import json
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform == 'linux':
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    from llguidance import LLMatcher, LLParserLimits, LLTokenizer, TokenizerWrapper, grammar_from

    class ByteTokenizer:
        eos_token_id = 256
        bos_token_id = None
        tokens = [bytes([i]) for i in range(256)] + [b'<eos>']
        special_token_ids = [256]

        def __call__(self, value):
            return list(value.encode('utf-8') if isinstance(value, str) else value)

    data = json.loads(sys.stdin.buffer.read(1_048_576))
    grammar = data['grammar']
    tokenizer = LLTokenizer(TokenizerWrapper(ByteTokenizer()), slices=[])
    limits = LLParserLimits(max_grammar_size=32768, max_lexer_states=10000,
                            max_items_in_row=2000, verbose_errors=False)
    compiled = grammar_from(grammar['syntax'], grammar['definition'])
    matcher = LLMatcher(tokenizer, compiled, limits=limits, log_level=0)
    if matcher.is_error():
        return {'error': 'Invalid or unsupported grammar: ' + matcher.get_error()[:400]}
    text = data['text']
    if text is None:
        return {'matches': True}
    accepted = matcher.consume_tokens(list(text.encode('utf-8')))
    return {'matches': accepted and not matcher.is_error() and matcher.is_accepting()}


if __name__ == '__main__':
    try:
        result = main()
    except Exception:
        result = {'error': 'Grammar parser could not process this grammar'}
    print(json.dumps(result, ensure_ascii=True))

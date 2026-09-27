# Image input and client tool results

The app-server backend accepts text and images through both API endpoints.
Images remain structured image parts when forwarded to the Worker; they are
never substituted with filenames, base64 text in prompts, or omission markers.
Image understanding depends on the configured model. The mock backend does not
interpret images.

## Responses

User message `content`, `function_call_output.output`, and
`custom_tool_call_output.output` can contain an ordered mixture of text and images:

```json
[
  {"type": "input_text", "text": "Viewed image history-desktop.png"},
  {"type": "input_image", "image_url": "data:image/png;base64,..."}
]
```

Supply real base64 bytes in place of `...`. HTTP(S) image URLs are also accepted.
Base64 data URLs support PNG, JPEG, WEBP and GIF. The gateway forwards URLs without
fetching them; HTTP(S) images must be accessible to the upstream image consumer.
Image-only input and multiple images are supported. Plain string tool outputs
continue to work.

Tool results must return the pending `call_id` with the same API key. They resume
the existing Worker tool call, using app-server `contentItems` with `inputText`
and `inputImage` (`imageUrl`), preserving part order.

## Chat Completions

Message content supports the usual shape:

```json
[
  {"type": "text", "text": "Describe this screenshot"},
  {"type": "image_url", "image_url": {"url": "https://example.org/screenshot.png", "detail": "high"}}
]
```

The adapter preserves image parts, including results in `role: "tool"` messages
with `tool_call_id`. Message images map to app-server `image` input items.
`detail` values `auto`, `low`, `high`, and `original` are passed through for turn
input. App-server dynamic tool results have no `detail` field, so image detail
on a pending tool result is not forwarded.

## History and limits

Full-history replay keeps images in their original positions. Automatic thread
continuation hashes image content/URLs and detail together with text; editing an
earlier image invalidates the history prefix. A successful continuation forwards
only the new input, including its images. If automatic resume fails before the
turn starts, the full original input is rebuilt with its images.

The default `CODEX_GATEWAY_MAX_REQUEST_BYTES` is 20971520 (20 MiB), including JSON,
base64 expansion and history. Existing deployments with an explicit 1048576
override must update it to allow larger screenshots; align reverse-proxy body
limits too. This setting is not the upstream model's image-size limit.

File IDs, local filesystem paths, files, audio, image generation and native
Worker `view_image` execution are not enabled by this change. Clients must read
local images and send their data URLs. Invalid image parts produce a request
error instead of being dropped. Gateway persistence continues to exclude input
content, storing only history digests for continuation.

Protocol reference: [OpenAI app-server documentation](https://developers.openai.com/codex/app-server).
Wire fields were checked against locally generated app-server TypeScript types.

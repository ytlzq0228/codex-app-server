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

## Gemini image input

Gemini supports image understanding through `/v1/responses`, `/v1/chat/completions`
and `/v1beta/models/{model}:generateContent` (also `streamGenerateContent`).
Send OpenAI image parts as above, or Gemini native parts such as:

```json
{"contents":[{"role":"user","parts":[
  {"text":"Describe this image"},
  {"inlineData":{"mimeType":"image/png","data":"<base64 image bytes>"}}
]}]}
```

Native `fileData` accepts a public HTTP(S) `fileUri` and image `mimeType`.
PNG, JPEG, WEBP and GIF are accepted, up to 8 images, 10 MiB per image and
20 MiB total decoded image content per turn. The gateway request-body limit
still applies to base64 JSON. URL downloads allow ports 80/443, verify TLS,
restrict redirects and resolved addresses to public IPs, and send no credentials.
Private-network URLs, local files and Gemini Files API `gs://` URIs are unsupported.
`detail` is accepted for compatibility; Gemini chooses its image resolution.

The pinned Antigravity CLI only accepts text blocks on streaming stdin
([official protocol](https://antigravity.google/docs/cli/headless/)). The Worker
therefore exposes images as a private, per-turn MCP `gateway_read_image` tool;
image bytes are returned as MCP image content, not embedded into the text prompt.
Images keep their order and continuation only forwards new attachments. Every
attachment must be read before the Worker emits an answer or invokes client tools.
These internal reads do not become public client tool calls. Upgrade both gateway
and all Gemini Workers; an older Worker returns an explicit capability error.

Gemini client tool results remain text-only. This feature supports image input
and text output, not image generation or editing.


## Claude images

Claude receives native Anthropic base64 image blocks, without an image-reading
tool. OpenAI URL/data-image input and Anthropic `image.source` base64/URL input
are converted to the same Worker content. Images in client tool results are also
supported.

The gateway accepts PNG/JPEG/GIF/WEBP, checks their MIME signatures, caps each
image at 10 MiB and each turn at 20 images / 20 MiB decoded. The configured total
request-byte limit still applies to base64 JSON. Remote downloads reuse the
public-IP-only resolver, redirect validation, TLS verification, timeout and byte
limits; no Worker or gateway credentials are sent to the image host. Documents,
PDFs, file IDs and private-network image URLs are unsupported.

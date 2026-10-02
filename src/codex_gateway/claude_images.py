"""Anthropic image blocks using the gateway's bounded, SSRF-safe downloader."""
import asyncio
import base64
import http.client

from .client_tools import ToolProtocolError
from .gemini_images import download_image, image_payload, MAX_IMAGE_BYTES, MAX_TOTAL_BYTES


async def content_blocks(parts):
    blocks, total, count = [], 0, 0
    try:
        for part in parts:
            if part["type"] == "text":
                blocks.append({"type": "text", "text": part["text"]})
                continue
            count += 1
            if count > 20:
                raise ValueError("Claude accepts at most 20 images per turn")
            url = part["url"]
            if url.startswith("data:"):
                header, data = url.split(",", 1)
                if len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                    raise ValueError("Images must not exceed 10 MiB")
                image = image_payload(base64.b64decode(data, validate=True), header[5:].split(";")[0])
            else:
                image = await asyncio.wait_for(asyncio.to_thread(download_image, url), 25)
            total += len(image["data"]) // 4 * 3 - (len(image["data"]) - len(image["data"].rstrip("=")))
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Image input must not exceed 20 MiB per turn")
            blocks.append({"type": "image", "source": {"type": "base64",
                           "media_type": image["mimeType"], "data": image["data"]}})
    except (ValueError, OSError, http.client.HTTPException, asyncio.TimeoutError) as exc:
        raise ToolProtocolError("Invalid Claude image input: " + str(exc), "invalid_image") from exc
    return blocks


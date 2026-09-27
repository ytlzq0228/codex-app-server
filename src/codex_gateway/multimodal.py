"""Lossless text/image adapters for public input and app-server messages."""
import base64
import binascii
from urllib.parse import urlsplit

TEXT_TYPES = {"text", "input_text", "output_text"}
IMAGE_TYPES = {"input_image", "image_url"}


def image_part(part):
    value = part.get("image_url")
    detail = part.get("detail")
    if part.get("type") == "image_url" and isinstance(value, dict):
        detail = value.get("detail", detail)
        value = value.get("url")
    if part.get("file_id") is not None:
        raise ValueError("Image file_id is unsupported; send an image URL or base64 data URL")
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Image requires a non-empty image_url")
    if value.startswith("data:"):
        header, separator, data = value.partition(",")
        if header.lower() not in {"data:image/png;base64", "data:image/jpeg;base64",
                                 "data:image/jpg;base64", "data:image/webp;base64", "data:image/gif;base64"} or not separator or not data:
            raise ValueError("Image data URL must contain base64 PNG, JPEG, WEBP or GIF data")
        try:
            base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Image data URL contains invalid base64") from None
    else:
        try:
            url = urlsplit(value)
            valid = url.scheme in {"http", "https"} and bool(url.hostname)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Image URL must use HTTP(S) or a base64 data URL; local paths are unsupported")
    if detail is not None and detail not in ("auto", "low", "high", "original"):
        raise ValueError("Unsupported image detail")
    result = {"type": "input_image", "image_url": value}
    if detail is not None:
        result["detail"] = detail
    return result


def content_parts(content):
    """Canonical public parts, preserving order and rejecting unknown modalities."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        raise ValueError("Content must be text or an array of text/image parts")
    result = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("Content parts must be objects")
        kind = part.get("type")
        if kind is not None and not isinstance(kind, str):
            raise ValueError("Content part type must be a string")
        if kind in TEXT_TYPES or (kind is None and isinstance(part.get("text"), str)):
            if not isinstance(part.get("text"), str):
                raise ValueError("Text content requires a string text field")
            result.append({"type": "input_text", "text": part["text"]})
        elif kind in IMAGE_TYPES:
            result.append(image_part(part))
        else:
            raise ValueError("Only text and image content parts are supported")
    return result


def dynamic_output(content):
    # Dynamic tool results have no detail field in the app-server protocol.
    return [{"type": "inputText", "text": p["text"]} if p["type"] == "input_text"
            else {"type": "inputImage", "imageUrl": p["image_url"]}
            for p in content_parts(content)]


def user_parts(content):
    return [{"type": "text", "text": p["text"]} if p["type"] == "input_text"
            else {"type": "image", "url": p["image_url"],
                  **({"detail": p["detail"]} if "detail" in p else {})}
            for p in content_parts(content)]

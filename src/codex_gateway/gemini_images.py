"""Bounded image transport for Gemini's per-turn MCP attachment reader."""
import asyncio
import base64
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 8


def image_payload(data, mime):
    mime = mime.lower().split(';')[0].strip().replace('image/jpg', 'image/jpeg')
    signatures = {
        'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': data.startswith(b'\xff\xd8\xff'),
        'image/gif': data.startswith((b'GIF87a', b'GIF89a')),
        'image/webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP',
    }
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Gemini images must be between 1 byte and 10 MiB')
    if not signatures.get(mime):
        raise ValueError('Image content must match its PNG, JPEG, WEBP or GIF MIME type')
    return {'mimeType': mime, 'data': base64.b64encode(data).decode('ascii')}


def download_image(url):
    """Resolve once per redirect and connect only to validated public IPs.

    No proxies, cookies or gateway credentials are sent. TLS still verifies the
    original hostname; connecting to the validated IP prevents DNS rebinding.
    """
    deadline = time.monotonic() + 20
    for _ in range(4):
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError('Image URL must be a public HTTP(S) URL without credentials')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if port not in {80, 443}:
            raise ValueError('Image URL must use port 80 or 443')
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Image URL must resolve to public IP addresses')
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise ValueError('Image download timed out')
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        response = None
        try:
            connection.sock = socket.create_connection((addresses[0][4][0], port), timeout=timeout)
            if parsed.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parsed.hostname)
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={'Accept': 'image/png,image/jpeg,image/webp,image/gif'})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Image redirect is missing a location')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError('Image URL did not return HTTP 200')
            data = bytearray()
            while len(data) <= MAX_IMAGE_BYTES:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise ValueError('Image download timed out')
                # The response retains the socket when HTTPConnection detaches it.
                chunk = response.read1(min(65536, MAX_IMAGE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            return image_payload(bytes(data), response.getheader('Content-Type', ''))
        finally:
            if response is not None:
                response.close()
            connection.close()
    raise ValueError('Too many image URL redirects')


async def prepare_images(parts):
    images, text = [], []
    total = 0
    for part in parts:
        if part['type'] == 'text':
            text.append(part['text'])
            continue
        if len(images) >= MAX_IMAGES:
            raise ValueError('Gemini accepts at most 8 images per turn')
        url = part['url']
        if url.startswith('data:'):
            header, data = url.split(',', 1)
            if len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                raise ValueError('Gemini images must not exceed 10 MiB')
            image = image_payload(base64.b64decode(data, validate=True), header[5:].split(';')[0])
        else:
            try:
                image = await asyncio.wait_for(asyncio.to_thread(download_image, url), 25)
            except (OSError, http.client.HTTPException, asyncio.TimeoutError) as exc:
                raise ValueError('Unable to download image from its public URL') from exc
        total += len(image['data']) // 4 * 3 - len(image['data']) + len(image['data'].rstrip('='))
        if total > MAX_TOTAL_BYTES:
            raise ValueError('Gemini image input must not exceed 20 MiB per turn')
        images.append(image)
        text.append(f'\n[Attached image {len(images)}: inspect with gateway_read_image(index={len(images)})]\n')
    return '\n'.join(text), images

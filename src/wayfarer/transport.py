"""Minimal HTTP transport. Every network call in the package goes through a ``Transport`` so tests can fake it."""

from __future__ import annotations

import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from wayfarer.errors import UpstreamError

USER_AGENT = "wayfarer/0.1"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Status and raw body. Raw bytes are kept because TEE signatures cover the exact bytes."""

    status: int
    body: bytes


Transport = Callable[[urllib.request.Request, float], HttpResponse]


def urllib_transport(request: urllib.request.Request, timeout: float) -> HttpResponse:
    """Send a request with ``urllib``. HTTP error statuses are returned, network failures raise ``UpstreamError``."""
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https URLs only)
            return HttpResponse(response.status, response.read())
    except urllib.error.HTTPError as error:
        return HttpResponse(error.code, error.read())
    except (TimeoutError, OSError) as error:  # URLError is an OSError
        raise UpstreamError(f"{request.host} did not answer in time ({type(error).__name__}).") from error


def build_request(
    url: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    method: str | None = None,
) -> urllib.request.Request:
    """Create a request; a body implies POST unless ``method`` says otherwise."""
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    return urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)  # noqa: S310


def encode_multipart(
    fields: Mapping[str, str], file_field: str, filename: str, content_type: str, data: bytes
) -> tuple[bytes, str]:
    """Encode a ``multipart/form-data`` body. Returns ``(body, content_type_header)``."""
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    chunks.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    chunks.append(data)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

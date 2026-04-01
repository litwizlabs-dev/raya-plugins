"""Hub URL normalization, path constants, and joining via :mod:`urllib.parse`.

Public constants include ``DEFAULT_HUB_URL`` and path segments such as
``PATH_TTS_SYNTHESIZE``, ``PATH_TRANSCRIBE``, etc., used to build full endpoint URLs.
"""

from __future__ import annotations

import os
from urllib.parse import urljoin, urlparse, urlunparse

DEFAULT_HUB_URL = "https://hub.getraya.app"

# REST paths (TTS)
PATH_TTS_SYNTHESIZE = "/v1/text-to-speech"
PATH_TTS_STREAM = "/v1/text-to-speech/stream"
PATH_TTS_VOICES = "/v1/voices"

# STT
PATH_TRANSCRIBE = "/transcribe"


def _ensure_absolute_http_url(raw: str) -> str:
    """Normalize a user-supplied string into an ``http`` or ``https`` URL without query/fragment.

    If no scheme is present, ``https://`` is prepended. Path is trimmed of a trailing
    slash (except the root).

    Args:
        raw: User or default hub base string.

    Returns:
        Normalized URL string (scheme + netloc + path only).

    Raises:
        ValueError: If the string is empty, the scheme is not ``http``/``https``,
            or there is no host (``netloc``).
    """
    s = raw.strip()
    if not s:
        raise ValueError("empty hub URL")
    if "://" not in s:
        s = "https://" + s
    p = urlparse(s)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"unsupported hub URL scheme: {p.scheme!r}")
    if not p.netloc:
        raise ValueError(f"invalid hub URL: {raw!r}")
    path = (p.path or "").rstrip("/")
    return urlunparse((p.scheme, p.netloc, path, "", "", ""))


def resolve_hub_base_url(
    explicit: str | None,
    *env_keys: str,
    default: str = DEFAULT_HUB_URL,
) -> str:
    """Return the hub base URL from the first non-empty source.

    Tries ``explicit``, then each ``os.environ[key]`` for ``env_keys`` in order.
    If all are empty, ``default`` is normalized.

    Args:
        explicit: Optional URL from the caller (e.g. constructor ``base_url``).
        *env_keys: Environment variable names to consult in order.
        default: Fallback when every candidate is empty.

    Returns:
        Normalized base URL without a trailing slash.

    Raises:
        ValueError: If normalization fails (see :func:`_ensure_absolute_http_url`).
    """
    candidates = [explicit, *(os.environ.get(k) for k in env_keys)]
    value = next((str(v).strip() for v in candidates if v and str(v).strip()), None)
    if not value:
        return _ensure_absolute_http_url(default).rstrip("/")
    return _ensure_absolute_http_url(value).rstrip("/")


def hub_url_join(base: str, path: str) -> str:
    """Append ``path`` to ``base`` using :func:`urllib.parse.urljoin`.

    Leading slashes on ``path`` are stripped so a path prefix on ``base`` is kept
    (for example ``https://host/api`` + ``/v1/voices`` → ``https://host/api/v1/voices``).

    Args:
        base: Hub base URL (with optional path prefix).
        path: Relative path segment (with or without a leading slash).

    Returns:
        Absolute URL string.
    """
    rel = path.strip().lstrip("/")
    if not rel:
        return base.rstrip("/")
    base_root = base.rstrip("/") + "/"
    return urljoin(base_root, rel)


def http_to_ws_origin(http_url: str) -> str:
    """Map an ``http`` or ``https`` URL to ``ws`` or ``wss`` with the same host and path.

    Args:
        http_url: HTTP(S) hub base; a scheme may be omitted (``https`` is assumed).

    Returns:
        WebSocket origin URL without query or fragment.

    Raises:
        ValueError: If the scheme is not ``http``/``https`` or the host is missing.
    """
    p = urlparse(http_url if "://" in http_url else f"https://{http_url}")
    if p.scheme == "https":
        scheme = "wss"
    elif p.scheme == "http":
        scheme = "ws"
    else:
        raise ValueError(f"unsupported URL scheme for WebSocket: {p.scheme!r}")
    if not p.netloc:
        raise ValueError(f"invalid hub URL for WebSocket: {http_url!r}")
    path = p.path or ""
    return urlunparse((scheme, p.netloc, path.rstrip("/"), "", "", "")).rstrip("/")


def transcribe_http_url(base_url: str) -> str:
    """Build the batch STT ``POST`` URL (multipart WAV).

    Args:
        base_url: Normalized hub base URL.

    Returns:
        Full ``https://.../transcribe`` URL (path joined per :func:`hub_url_join`).
    """
    return hub_url_join(base_url, PATH_TRANSCRIBE)


def transcribe_ws_url(base_url: str) -> str:
    """Build the streaming STT WebSocket URL.

    Args:
        base_url: Normalized hub base URL.

    Returns:
        Full ``wss://.../transcribe`` URL.
    """
    return hub_url_join(http_to_ws_origin(base_url), PATH_TRANSCRIBE)


def tts_synthesize_url(base_url: str) -> str:
    """Build the non-streaming TTS synthesis endpoint URL.

    Args:
        base_url: Normalized hub base URL.

    Returns:
        Full URL for ``POST /v1/text-to-speech``.
    """
    return hub_url_join(base_url, PATH_TTS_SYNTHESIZE)


def tts_stream_url(base_url: str) -> str:
    """Build the streaming TTS (SSE) endpoint URL.

    Args:
        base_url: Normalized hub base URL.

    Returns:
        Full URL for ``POST /v1/text-to-speech/stream``.
    """
    return hub_url_join(base_url, PATH_TTS_STREAM)


def tts_voices_url(base_url: str) -> str:
    """Build the voice list ``GET`` URL.

    Args:
        base_url: Normalized hub base URL.

    Returns:
        Full URL for ``GET /v1/voices``.
    """
    return hub_url_join(base_url, PATH_TTS_VOICES)

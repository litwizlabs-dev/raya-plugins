"""Hub URL normalization, path constants, and joining via :mod:`urllib.parse`."""

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
    """Pick the first non-empty value from ``explicit`` and ``os.environ[key]`` keys."""
    candidates = [explicit, *(os.environ.get(k) for k in env_keys)]
    value = next((str(v).strip() for v in candidates if v and str(v).strip()), None)
    if not value:
        return _ensure_absolute_http_url(default).rstrip("/")
    return _ensure_absolute_http_url(value).rstrip("/")


def hub_url_join(base: str, path: str) -> str:
    """Append ``path`` to ``base`` using :func:`urllib.parse.urljoin`.

    ``path`` may be ``/v1/foo`` or ``v1/foo``; a path prefix on ``base`` is preserved
    (e.g. ``https://host/api`` + ``/v1/voices`` → ``https://host/api/v1/voices``).
    """
    rel = path.strip().lstrip("/")
    if not rel:
        return base.rstrip("/")
    base_root = base.rstrip("/") + "/"
    return urljoin(base_root, rel)


def http_to_ws_origin(http_url: str) -> str:
    """Map ``http``/``https`` origin to ``ws``/``wss`` (same host and path, no query)."""
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
    """``POST`` target for batch STT (multipart WAV)."""
    return hub_url_join(base_url, PATH_TRANSCRIBE)


def transcribe_ws_url(base_url: str) -> str:
    """WebSocket URL for streaming STT (base64 WAV per message)."""
    return hub_url_join(http_to_ws_origin(base_url), PATH_TRANSCRIBE)


def tts_synthesize_url(base_url: str) -> str:
    """Non-streaming TTS synthesis endpoint."""
    return hub_url_join(base_url, PATH_TTS_SYNTHESIZE)


def tts_stream_url(base_url: str) -> str:
    """Streaming TTS SSE/Web endpoint."""
    return hub_url_join(base_url, PATH_TTS_STREAM)


def tts_voices_url(base_url: str) -> str:
    """Voice list endpoint."""
    return hub_url_join(base_url, PATH_TTS_VOICES)

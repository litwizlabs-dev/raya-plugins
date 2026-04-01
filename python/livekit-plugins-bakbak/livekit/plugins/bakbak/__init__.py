"""LiveKit Agents plugins for Bakbak / Raya (hub at ``https://hub.getraya.app``).

Exports:

    * ``TTS`` — text-to-speech (REST + streaming SSE).
    * ``STT`` — speech-to-text (multipart HTTP + WebSocket).
    * Types: ``BakbakCodec``, ``BakbakLanguage``, ``SampleRate``, ``BakbakSTTLanguage``.

Authentication and base URL use ``BAKBAK_API_KEY`` / ``RAYA_API_KEY`` and
``BAKBAK_BASE_URL`` / ``RAYA_API_BASE_URL`` when not passed explicitly.
"""
from __future__ import annotations

from .stt import STT, BakbakSTTLanguage
from .tts import (
    TTS,
    BakbakCodec,
    BakbakLanguage,
    SampleRate,
)

__all__ = [
    "STT",
    "BakbakSTTLanguage",
    "TTS",
    "BakbakCodec",
    "BakbakLanguage",
    "SampleRate",
]

__version__ = "0.1.0"

# Fail loudly if livekit-agents is too old rather than producing cryptic
# AttributeErrors at runtime.
_MIN_AGENTS_VERSION = (0, 12, 0)

try:
    from livekit.agents.version import __version__ as _agents_version

    _av = tuple(int(x) for x in _agents_version.split(".")[:3])
    if _av < _MIN_AGENTS_VERSION:
        import warnings

        warnings.warn(
            f"livekit-agents {_agents_version} is older than the minimum required "
            f"{'.'.join(str(x) for x in _MIN_AGENTS_VERSION)} for livekit-plugins-bakbak. "
            "Some features may not work correctly.",
            RuntimeWarning,
            stacklevel=2,
        )
except Exception:
    pass

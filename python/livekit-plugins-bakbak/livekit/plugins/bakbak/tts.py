from __future__ import annotations

import array
import asyncio
import base64
import io
import json
import os
import time
import wave
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, Optional, Union

import aiohttp

from livekit.agents import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    tokenize,
    tts,
    utils,
)
from livekit.agents.types import (
    APIConnectOptions,
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    NotGivenOr,
)
from livekit.agents.tts import SentenceStreamPacer
from livekit.agents.utils import is_given
from livekit.agents.metrics import TTSMetrics

from .log import logger

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

API_KEY_HEADER = "X-API-Key"
DEFAULT_BASE_URL = "https://hub.getraya.app"

_PATH_SYNTHESIZE = "/v1/text-to-speech"
_PATH_STREAM = "/v1/text-to-speech/stream"
_PATH_VOICES = "/v1/voices"

_CHUNKED_TIMEOUT = aiohttp.ClientTimeout(total=120, sock_connect=10)
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10)

# ~32 KB ≈ ~340 ms at 24 kHz mono s16le
_PCM_CHUNK_BYTES = 32_000

# Retry / backoff
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0   # seconds; doubled each attempt

# Voices cache TTL
_VOICES_CACHE_TTL = 3600  # 1 hour

BakbakCodec = Literal["pcm", "wav", "mp3", "mulaw"]
BakbakLanguage = Literal["hi", "mr", "te", "kn", "bn"]
SampleRate = Literal[8000, 16000, 22050, 24000]

# numpy optional — strongly recommended for fast PCM conversion
try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]
    _NUMPY_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_base_url(explicit: Optional[str]) -> str:
    candidates = [explicit, os.environ.get("BAKBAK_BASE_URL"), os.environ.get("RAYA_API_BASE_URL")]
    value = next((v.strip() for v in candidates if v and v.strip()), None)
    return value.strip("/ \t\n") if value else DEFAULT_BASE_URL.rstrip("/")


def _resolve_api_key(explicit: Optional[str]) -> str:
    candidates = [explicit, os.environ.get("BAKBAK_API_KEY"), os.environ.get("RAYA_API_KEY")]
    value = next((v.strip() for v in candidates if v and v.strip()), None)
    if not value:
        raise ValueError(
            "Bakbak API key required: pass api_key= or set BAKBAK_API_KEY / RAYA_API_KEY."
        )
    return value


def _f32le_to_s16le(data: bytes) -> bytes:
    """Convert raw F32LE PCM bytes to S16LE. Uses numpy when available."""
    if len(data) % 4:
        raise APIError(f"F32LE PCM length {len(data)} is not a multiple of 4", retryable=False)
    if _NUMPY_AVAILABLE:
        f32 = np.frombuffer(data, dtype=np.float32)
        return (np.clip(f32, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    floats = array.array("f")
    floats.frombytes(data)
    return array.array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in floats)).tobytes()


def _pcm_from_wav(body: bytes, configured_rate: int) -> tuple[memoryview, int]:
    """Parse a WAV body and return (pcm_memory_view, sample_rate)."""
    try:
        with wave.open(io.BytesIO(body), "rb") as wf:
            ch, sr, sw = wf.getnchannels(), wf.getframerate(), wf.getsampwidth()
            if ch != 1:
                raise APIError(f"expected mono WAV from Bakbak, got {ch} channels", retryable=False)
            if sw != 2:
                raise APIError(f"expected 16-bit WAV from Bakbak, got sample width {sw}", retryable=False)
            if sr != configured_rate:
                logger.warning(
                    "Bakbak WAV sample rate %d differs from configured %d; using %d",
                    sr, configured_rate, sr,
                )
            return memoryview(wf.readframes(wf.getnframes())), sr
    except wave.Error as exc:
        raise APIError(f"invalid WAV from Bakbak: {exc}", retryable=False) from exc


async def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """Read the error body and raise a typed APIStatusError."""
    if resp.status < 400:
        return
    text = await resp.text()
    detail: object = text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "detail" in parsed:
            detail = parsed
            text = str(parsed["detail"])
    except json.JSONDecodeError:
        pass
    if resp.status == 429:
        raise APIStatusError(
            message="Bakbak rate limit exceeded — back off and retry",
            status_code=429,
            body=detail,
        )
    raise APIStatusError(message=text or resp.reason, status_code=resp.status, body=detail)


async def _post_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: aiohttp.ClientTimeout,
) -> aiohttp.ClientResponse:
    """POST with exponential backoff on 429 and transient 5xx errors."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await session.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES - 1:
                await resp.release()
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Bakbak request got %d; retrying in %.1fs (attempt %d/%d)",
                    resp.status, delay, attempt + 1, _MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue
            return resp
        except asyncio.TimeoutError:
            raise APITimeoutError() from None
        except aiohttp.ClientError as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Bakbak connection error: %s; retrying in %.1fs (attempt %d/%d)",
                    exc, delay, attempt + 1, _MAX_RETRIES,
                )
                await asyncio.sleep(delay)
            continue
    raise APIConnectionError() from last_exc


# ─────────────────────────────────────────────────────────────────────────────
# Options
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _TTSOptions:
    api_key: str
    base_url: str
    voice_id: str
    language: str
    model: str
    speed: float
    sample_rate: SampleRate
    rest_codec: BakbakCodec

    @property
    def auth_headers(self) -> dict[str, str]:
        return {API_KEY_HEADER: self.api_key, "Content-Type": "application/json"}

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def request_json(self, text: str, *, codec: Optional[BakbakCodec] = None) -> dict[str, Any]:
        return {
            "text": text,
            "voice_id": self.voice_id,
            "language": self.language,
            "model": self.model,
            "speed": self.speed,
            "sample_rate": self.sample_rate,
            "codec": codec or self.rest_codec,
        }


# ─────────────────────────────────────────────────────────────────────────────
# TTS
# ─────────────────────────────────────────────────────────────────────────────


class TTS(tts.TTS):
    """LiveKit Agents plugin for Bakbak TTS.

    Args:
        voice_id:     Bakbak voice identifier.
        language:     BCP-47 language code (``"hi"``, ``"mr"``, ``"te"``, ...).
        api_key:      Bakbak / Raya API key. Falls back to ``BAKBAK_API_KEY``
                      or ``RAYA_API_KEY`` env vars.
        model:        Synthesis model (default ``"standard"``).
        speed:        Speech speed multiplier (default ``1.0``).
        sample_rate:  Output sample rate in Hz (default ``24,000``).
        base_url:     Override hub URL. Falls back to ``BAKBAK_BASE_URL`` /
                      ``RAYA_API_BASE_URL`` / ``https://hub.getraya.app``.
        rest_codec:   Audio codec for non-streaming synthesis (default ``"wav"``).
        http_session: Inject an existing ``aiohttp.ClientSession``.
                      **Lifecycle note**: the caller owns the injected session
                      and is responsible for closing it. ``TTS.aclose()`` will
                      *not* close an externally supplied session.
        tokenizer:    Sentence tokenizer for the streaming path.
        text_pacing:  Enable sentence-level pacing on the stream path.
    """

    def __init__(
        self,
        *,
        voice_id: str,
        language: Union[BakbakLanguage, str],
        api_key: Optional[str] = None,
        model: str = "standard",
        speed: float = 1.0,
        sample_rate: SampleRate = 24000,
        base_url: Optional[str] = None,
        rest_codec: BakbakCodec = "wav",
        http_session: Optional[aiohttp.ClientSession] = None,
        tokenizer: NotGivenOr[tokenize.SentenceTokenizer] = NOT_GIVEN,
        text_pacing: Union[SentenceStreamPacer, bool] = False,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=int(sample_rate),
            num_channels=1,
        )
        if not 0.5 <= speed <= 1.5:
            raise ValueError(f"speed must be between 0.5 and 1.5, got {speed}")
        self._opts = _TTSOptions(
            api_key=_resolve_api_key(api_key),
            base_url=_resolve_base_url(base_url),
            voice_id=voice_id,
            language=str(language),
            model=model,
            speed=speed,
            sample_rate=sample_rate,
            rest_codec=rest_codec,
        )
        self._session = http_session
        self._own_session = False
        self._streams: weakref.WeakSet[SynthesizeStream] = weakref.WeakSet()
        self._sentence_tokenizer = (
            tokenizer if is_given(tokenizer) else tokenize.blingfire.SentenceTokenizer()
        )
        self._stream_pacer = (
            text_pacing if isinstance(text_pacing, SentenceStreamPacer)
            else (SentenceStreamPacer() if text_pacing is True else None)
        )
        self._voices_cache: Optional[list[dict[str, Any]]] = None
        self._voices_cache_at: float = 0.0
        self._voices_lock = asyncio.Lock()

    # ── Public properties ────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Bakbak"

    @property
    def options(self) -> _TTSOptions:
        return self._opts

    @property
    def sentence_tokenizer(self) -> tokenize.SentenceTokenizer:
        return self._sentence_tokenizer

    @property
    def stream_pacer(self) -> Optional[SentenceStreamPacer]:
        return self._stream_pacer

    # ── Session management ───────────────────────────────────────────────────

    def ensure_session(self) -> aiohttp.ClientSession:
        """Return a shared ``aiohttp.ClientSession``, creating one if needed."""
        if self._session is not None:
            return self._session
        try:
            self._session = utils.http_context.http_session()
        except RuntimeError:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    # ── LiveKit TTS interface ────────────────────────────────────────────────

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> SynthesizeStream:
        s = SynthesizeStream(tts=self, conn_options=conn_options)
        self._streams.add(s)
        return s

    def update_options(
        self,
        *,
        voice_id: Optional[str] = None,
        language: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        sample_rate: Optional[SampleRate] = None,
        rest_codec: Optional[BakbakCodec] = None,
    ) -> None:
        """Update TTS options at runtime without reconstructing the object.

        Only the provided (non-None) arguments are changed. Useful for
        swapping voice or language mid-session.
        """
        if speed is not None and not 0.5 <= speed <= 1.5:
            raise ValueError(f"speed must be between 0.5 and 1.5, got {speed}")
        self._opts = replace(
            self._opts,
            **{k: v for k, v in {
                "voice_id": voice_id,
                "language": language,
                "model": model,
                "speed": speed,
                "sample_rate": sample_rate,
                "rest_codec": rest_codec,
            }.items() if v is not None},
        )

    async def list_voices(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Return available voices, using a 1-hour in-memory cache.

        Args:
            force_refresh: Bypass the cache and fetch from the API.
        """
        async with self._voices_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._voices_cache is not None
                and now - self._voices_cache_at < _VOICES_CACHE_TTL
            ):
                return self._voices_cache

            opts = self._opts
            try:
                async with self.ensure_session().get(
                    opts.url(_PATH_VOICES),
                    headers=opts.auth_headers,
                    timeout=aiohttp.ClientTimeout(total=30, sock_connect=10),
                ) as resp:
                    await _raise_for_status(resp)
                    data = await resp.json()
            except asyncio.TimeoutError:
                raise APITimeoutError() from None
            except aiohttp.ClientError as exc:
                raise APIConnectionError() from exc

            voices: list[dict[str, Any]] = data if isinstance(data, list) else data.get("voices", [])
            self._voices_cache = voices
            self._voices_cache_at = now
            logger.debug("Bakbak voices cache refreshed (%d voices)", len(voices))
            return voices

    async def aclose(self) -> None:
        if self._streams:
            await asyncio.gather(
                *(s.aclose() for s in list(self._streams)), return_exceptions=True
            )
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None


# ─────────────────────────────────────────────────────────────────────────────
# ChunkedStream  (non-streaming / batch endpoint)
# ─────────────────────────────────────────────────────────────────────────────


class ChunkedStream(tts.ChunkedStream):
    def __init__(
        self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts.options)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        opts = self._opts
        timeout = aiohttp.ClientTimeout(
            total=_CHUNKED_TIMEOUT.total,
            sock_connect=self._conn_options.timeout,
            sock_read=self._conn_options.timeout,
        )
        start_time = time.perf_counter()
        try:
            async with await _post_with_retry(
                self._tts.ensure_session(),
                opts.url(_PATH_SYNTHESIZE),
                headers=opts.auth_headers,
                payload=opts.request_json(self._input_text),
                timeout=timeout,
            ) as resp:
                await _raise_for_status(resp)
                body = await resp.read()
        except (APITimeoutError, APIConnectionError, APIStatusError):
            raise
        except aiohttp.ClientError as exc:
            raise APIConnectionError() from exc

        request_id = utils.shortuuid()
        duration = time.perf_counter() - start_time

        if opts.rest_codec == "wav":
            pcm, sample_rate = _pcm_from_wav(body, opts.sample_rate)
        else:
            if opts.rest_codec == "pcm" and len(body) % 2:
                raise APIError("PCM body length is not a multiple of 2", retryable=False)
            pcm, sample_rate = body, opts.sample_rate

        output_emitter.initialize(
            request_id=request_id,
            sample_rate=int(sample_rate),
            num_channels=1,
            mime_type="audio/pcm",
            stream=False,
        )
        pcm_len = len(pcm)
        if pcm_len <= _PCM_CHUNK_BYTES:
            output_emitter.push(bytes(pcm))
        else:
            for i in range(0, pcm_len, _PCM_CHUNK_BYTES):
                output_emitter.push(bytes(pcm[i : i + _PCM_CHUNK_BYTES]))
        output_emitter.flush()

        self._tts.emit(
            "metrics_collected",
            TTSMetrics(
                timestamp=time.time(),
                request_id=request_id,
                ttfb=duration,
                duration=duration,
                audio_duration=pcm_len / (int(sample_rate) * 2),  # s16le = 2 bytes/sample
                cancelled=False,
                label=self._tts.label,
                characters_count=len(self._input_text),
                streamed=False,
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# SynthesizeStream  (streaming / SSE endpoint)
# ─────────────────────────────────────────────────────────────────────────────


class SynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts.options)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._opts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id=utils.shortuuid())

        sent_stream = self._tts.sentence_tokenizer.stream()
        if self._tts.stream_pacer is not None:
            sent_stream = self._tts.stream_pacer.wrap(
                sent_stream=sent_stream, audio_emitter=output_emitter
            )

        timeout = aiohttp.ClientTimeout(
            total=_STREAM_TIMEOUT.total,
            sock_connect=self._conn_options.timeout,
            sock_read=self._conn_options.timeout,
        )
        try:
            await asyncio.gather(
                self._input_task(sent_stream),
                self._synth_task(sent_stream, output_emitter, timeout),
            )
        finally:
            output_emitter.end_segment()
            await sent_stream.aclose()

    async def _input_task(self, sent_stream: tokenize.SentenceStream) -> None:
        async for data in self._input_ch:
            if isinstance(data, self._FlushSentinel):
                sent_stream.flush()
            else:
                sent_stream.push_text(data)
        sent_stream.end_input()

    async def _synth_task(
        self,
        sent_stream: tokenize.SentenceStream,
        output_emitter: tts.AudioEmitter,
        timeout: aiohttp.ClientTimeout,
    ) -> None:
        opts = self._opts
        session = self._tts.ensure_session()
        url = opts.url(_PATH_STREAM)
        total_chars = 0

        async for ev in sent_stream:
            text = (ev.token or "").strip()
            if not text:
                continue
            self._mark_started()
            start_time = time.perf_counter()
            request_id = utils.shortuuid()
            ttfb = await _stream_utterance(
                session=session,
                url=url,
                headers=opts.auth_headers,
                payload=opts.request_json(text, codec="wav"),
                output_emitter=output_emitter,
                timeout=timeout,
                request_id=request_id,
            )
            duration = time.perf_counter() - start_time
            total_chars += len(text)
            self._tts.emit(
                "metrics_collected",
                TTSMetrics(
                    timestamp=time.time(),
                    request_id=request_id,
                    ttfb=ttfb,
                    duration=duration,
                    audio_duration=0.0,
                    cancelled=False,
                    label=self._tts.label,
                    characters_count=len(text),
                    streamed=True,
                ),
            )


# ─────────────────────────────────────────────────────────────────────────────
# Streaming helpers (module-level)
# ─────────────────────────────────────────────────────────────────────────────


async def _stream_utterance(
    *,
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    output_emitter: tts.AudioEmitter,
    timeout: aiohttp.ClientTimeout,
    request_id: str,
) -> float:
    """POST a single utterance to the streaming endpoint and consume SSE.

    Returns the time-to-first-byte (seconds) for metrics.
    """
    try:
        async with await _post_with_retry(
            session, url, headers=headers, payload=payload, timeout=timeout
        ) as resp:
            await _raise_for_status(resp)
            ct = resp.content_type or ""
            if ct and "text/event-stream" not in ct:
                logger.debug("unexpected stream content-type: %s", ct)
            ttfb = await _consume_sse(resp, output_emitter)
            return ttfb
    except (APITimeoutError, APIConnectionError, APIStatusError):
        raise
    except aiohttp.ClientError as exc:
        raise APIConnectionError() from exc


async def _consume_sse(
    resp: aiohttp.ClientResponse,
    output_emitter: tts.AudioEmitter,
) -> float:
    """Read an SSE response using ``readline`` and return time-to-first-byte."""
    return await _consume_sse_readline(resp.content.readline, output_emitter)


async def _consume_sse_readline(
    readline: Callable[[], Awaitable[bytes]],
    output_emitter: tts.AudioEmitter,
) -> float:
    """Read an SSE response line-by-line and dispatch decoded PCM chunks.

    Handles two SSE payload styles from Bakbak:
    - Style A: ``event: chunk`` field + ``data: {...}`` (event field as discriminator)
    - Style B: no ``event:`` field, ``data: {"type": "chunk", ...}`` (type key as discriminator)

    Skips SSE comment lines (``:`` …) per the SSE spec.
    Returns time-to-first-byte (seconds) measured from entry to first chunk push.
    """
    event: Optional[str] = None
    buf: list[str] = []
    ttfb: Optional[float] = None
    start = time.perf_counter()

    while True:
        line_b = await readline()
        if not line_b:
            break
        line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line == "":
            if buf:
                first_chunk = ttfb is None and _is_chunk_event(event, buf)
                await _handle_sse_event(event, "\n".join(buf), output_emitter)
                if first_chunk:
                    ttfb = time.perf_counter() - start
            event, buf = None, []
        elif line.startswith("event:"):
            if buf:
                await _handle_sse_event(event, "\n".join(buf), output_emitter)
            event, buf = line[6:].strip(), []
        elif line.startswith("data:"):
            buf.append(line[5:].lstrip())
        else:
            buf.append(line)

    if buf:
        await _handle_sse_event(event, "\n".join(buf), output_emitter)

    return ttfb if ttfb is not None else time.perf_counter() - start


def _is_chunk_event(event: Optional[str], buf: list[str]) -> bool:
    """Return True if this SSE event looks like an audio chunk (for TTFB tracking)."""
    if event == "chunk":
        return True
    if event is None and buf:
        try:
            payload = json.loads("\n".join(buf))
            return isinstance(payload, dict) and payload.get("type") == "chunk"
        except json.JSONDecodeError:
            pass
    return False


async def _handle_sse_event(
    ev: Optional[str],
    raw: str,
    output_emitter: tts.AudioEmitter,
) -> None:
    """Decode and dispatch a single SSE event.

    Supports both ``event:``-field discrimination (Style A) and
    ``{"type": ...}`` payload discrimination (Style B).
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise APIError(
            f"invalid JSON in Bakbak SSE event '{ev}': {exc}",
            body=raw,
            retryable=False,
        ) from exc

    # Resolve the effective event type: prefer the SSE event: field (Style A),
    # fall back to payload["type"] (Style B).
    etype = ev or (payload.get("type") if isinstance(payload, dict) else None)

    if etype == "chunk":
        b64 = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(b64, str):
            logger.debug("SSE 'chunk' event missing 'data' string field; skipping")
            return
        try:
            f32 = base64.b64decode(b64)
        except Exception as exc:
            raise APIError(f"invalid base64 in SSE chunk: {exc}", retryable=False) from exc
        output_emitter.push(_f32le_to_s16le(f32))
    elif etype == "done":
        pass
    elif etype == "error":
        msg = (payload.get("message") or payload.get("detail") or raw) if isinstance(payload, dict) else raw
        raise APIError(f"Bakbak stream error: {msg}", retryable=False)
    else:
        logger.debug("unknown Bakbak SSE event '%s'; ignoring", etype)
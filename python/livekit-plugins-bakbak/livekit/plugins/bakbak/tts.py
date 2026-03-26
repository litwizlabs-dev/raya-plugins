from __future__ import annotations

import array
import asyncio
import base64
import io
import json
import os
import wave
import weakref
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

from .log import logger

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

API_KEY_HEADER = "X-API-Key"
DEFAULT_BASE_URL = "https://hub.getraya.app"

# Endpoint paths — single source of truth
_PATH_SYNTHESIZE = "/v1/text-to-speech"
_PATH_STREAM = "/v1/text-to-speech/stream"

# Non-streaming: read the whole body in one shot, so a generous total timeout
# is more useful than a per-read timeout.
_CHUNKED_TIMEOUT = aiohttp.ClientTimeout(total=120, sock_connect=10)

# Streaming: no total timeout (unbounded stream), but we still want a
# connect guard so a hung server doesn't block forever.
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10)

# PCM output chunk size for output_emitter.push() calls (~32 KB ≈ ~340ms at 24 kHz mono s16le)
_PCM_CHUNK_BYTES = 32_000

BakbakCodec = Literal["pcm", "wav", "mp3", "mulaw"]
BakbakLanguage = Literal["hi", "mr", "te", "kn", "bn"]
SampleRate = Literal[8000, 16000, 22050, 24000]


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_base_url(explicit: Optional[str]) -> str:
    """Precedence: kwarg → BAKBAK_BASE_URL → RAYA_API_BASE_URL → default."""
    candidates = [
        explicit,
        os.environ.get("BAKBAK_BASE_URL"),
        os.environ.get("RAYA_API_BASE_URL"),
    ]
    for c in candidates:
        if c and c.strip():
            return c.strip().rstrip("/")
    return DEFAULT_BASE_URL.rstrip("/")


def _resolve_api_key(explicit: Optional[str]) -> str:
    """Precedence: kwarg → BAKBAK_API_KEY → RAYA_API_KEY."""
    candidates = [
        explicit,
        os.environ.get("BAKBAK_API_KEY"),
        os.environ.get("RAYA_API_KEY"),
    ]
    for c in candidates:
        if c and c.strip():
            return c.strip()
    raise ValueError(
        "Bakbak API key required: pass api_key= or set BAKBAK_API_KEY / RAYA_API_KEY."
    )


def _f32le_to_s16le(data: bytes) -> bytes:
    """Convert raw float-32 LE PCM bytes to signed-16 LE PCM bytes."""
    n = len(data)
    if n % 4:
        raise APIError(
            f"F32LE PCM length {n} is not a multiple of 4", retryable=False
        )
    floats = array.array("f")
    floats.frombytes(data)
    # Clamp then scale in one pass — avoids a second allocation
    out = array.array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in floats))
    return out.tobytes()


def _pcm_from_wav(body: bytes, configured_rate: int) -> tuple[bytes, int]:
    """
    Parse a WAV body, validate it is mono 16-bit, and return (pcm_bytes, sample_rate).
    Logs a warning when the WAV's embedded rate differs from the configured one.
    """
    try:
        with wave.open(io.BytesIO(body), "rb") as wf:
            ch = wf.getnchannels()
            sr = wf.getframerate()
            sw = wf.getsampwidth()

            if ch != 1:
                raise APIError(
                    f"expected mono WAV from Bakbak, got {ch} channels", retryable=False
                )
            if sw != 2:
                raise APIError(
                    f"expected 16-bit WAV from Bakbak, got sample width {sw}", retryable=False
                )
            if sr != configured_rate:
                logger.warning(
                    "Bakbak WAV sample rate %d differs from configured %d; using %d",
                    sr,
                    configured_rate,
                    sr,
                )
            return wf.readframes(wf.getnframes()), sr
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
    raise APIStatusError(message=text or resp.reason, status_code=resp.status, body=detail)


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

    # Pre-built auth headers — computed once, reused on every request
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
        language:     BCP-47 language code (``"hi"``, ``"mr"``, ``"te"``, …).
        api_key:      Bakbak / Raya API key. Falls back to ``BAKBAK_API_KEY``
                      or ``RAYA_API_KEY`` env vars.
        model:        Synthesis model (default ``"standard"``).
        speed:        Speech speed multiplier (default ``1.0``).
        sample_rate:  Output sample rate in Hz (default ``24000``).
        base_url:     Override hub URL. Falls back to ``BAKBAK_BASE_URL`` /
                      ``RAYA_API_BASE_URL`` / ``https://hub.getraya.app``.
        rest_codec:   Audio codec for non-streaming synthesis (default ``"wav"``).
        http_session: Inject an existing ``aiohttp.ClientSession``.
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

        self._sentence_tokenizer: tokenize.SentenceTokenizer = (
            tokenizer if is_given(tokenizer) else tokenize.blingfire.SentenceTokenizer()
        )
        self._stream_pacer: Optional[SentenceStreamPacer] = (
            text_pacing
            if isinstance(text_pacing, SentenceStreamPacer)
            else (SentenceStreamPacer() if text_pacing is True else None)
        )

    # ── Public properties ────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Bakbak"

    @property
    def options(self) -> _TTSOptions:
        """Hub URL, voice, language, and other request defaults."""
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
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> SynthesizeStream:
        s = SynthesizeStream(tts=self, conn_options=conn_options)
        self._streams.add(s)
        return s

    async def aclose(self) -> None:
        # Close all tracked streams concurrently
        if self._streams:
            await asyncio.gather(
                *(s.aclose() for s in list(self._streams)), return_exceptions=True
            )
            self._streams.clear()
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None


# ─────────────────────────────────────────────────────────────────────────────
# ChunkedStream  (non-streaming / batch endpoint)
# ─────────────────────────────────────────────────────────────────────────────


class ChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts.options)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        opts = self._opts
        payload = opts.request_json(self._input_text)
        timeout = aiohttp.ClientTimeout(
            total=_CHUNKED_TIMEOUT.total,
            sock_connect=self._conn_options.timeout,
            sock_read=self._conn_options.timeout,
        )

        try:
            async with self._tts.ensure_session().post(
                opts.url(_PATH_SYNTHESIZE),
                json=payload,
                headers=opts.auth_headers,
                timeout=timeout,
            ) as resp:
                await _raise_for_status(resp)
                body = await resp.read()
        except asyncio.TimeoutError:
            raise APITimeoutError() from None
        except aiohttp.ClientError as exc:
            raise APIConnectionError() from exc

        # Decode to raw PCM
        if opts.rest_codec == "wav":
            pcm, sample_rate = _pcm_from_wav(body, opts.sample_rate)
        else:
            pcm = body
            sample_rate = opts.sample_rate
            if opts.rest_codec == "pcm" and len(pcm) % 2:
                raise APIError("PCM body length is not a multiple of 2", retryable=False)

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=int(sample_rate),
            num_channels=1,
            mime_type="audio/pcm",
            stream=False,
        )
        # Push in fixed-size chunks to keep memory usage predictable
        for i in range(0, len(pcm), _PCM_CHUNK_BYTES):
            output_emitter.push(pcm[i : i + _PCM_CHUNK_BYTES])
        output_emitter.flush()


# ─────────────────────────────────────────────────────────────────────────────
# SynthesizeStream  (streaming / SSE endpoint)
# ─────────────────────────────────────────────────────────────────────────────


class SynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts.options)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        opts = self._opts
        request_id = utils.shortuuid()

        output_emitter.initialize(
            request_id=request_id,
            sample_rate=opts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id=utils.shortuuid())

        sent_stream = self._tts.sentence_tokenizer.stream()
        if self._tts.stream_pacer is not None:
            sent_stream = self._tts.stream_pacer.wrap(
                sent_stream=sent_stream,
                audio_emitter=output_emitter,
            )

        session = self._tts.ensure_session()
        url = opts.url(_PATH_STREAM)
        headers = opts.auth_headers

        async def _input_task() -> None:
            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    sent_stream.flush()
                else:
                    sent_stream.push_text(data)
            sent_stream.end_input()

        async def _synth_task() -> None:
            try:
                async for ev in sent_stream:
                    text = (ev.token or "").strip()
                    if not text:
                        continue
                    self._mark_started()
                    await _stream_utterance(
                        session=session,
                        url=url,
                        headers=headers,
                        payload=opts.request_json(text, codec="wav"),
                        output_emitter=output_emitter,
                        conn_options=self._conn_options,
                    )
            finally:
                await sent_stream.aclose()

        try:
            await asyncio.gather(_input_task(), _synth_task())
        finally:
            output_emitter.end_segment()


# ─────────────────────────────────────────────────────────────────────────────
# Streaming helpers (module-level — no need to be methods)
# ─────────────────────────────────────────────────────────────────────────────


async def _stream_utterance(
    *,
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    output_emitter: tts.AudioEmitter,
    conn_options: APIConnectOptions,
) -> None:
    timeout = aiohttp.ClientTimeout(
        total=_STREAM_TIMEOUT.total,
        sock_connect=conn_options.timeout,
        sock_read=conn_options.timeout,
    )
    try:
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            await _raise_for_status(resp)
            ct = resp.content_type or ""
            if ct and "text/event-stream" not in ct:
                logger.debug("unexpected stream content-type: %s", ct)
            await _consume_sse(resp, output_emitter)
    except asyncio.TimeoutError:
        raise APITimeoutError() from None
    except aiohttp.ClientError as exc:
        raise APIConnectionError() from exc


async def _consume_sse(
    resp: aiohttp.ClientResponse,
    output_emitter: tts.AudioEmitter,
) -> None:
    """
    Parse the SSE stream from Bakbak.

    Expected event format:
        event: chunk
        data: {"data": "<base64 F32LE PCM>"}

        event: done
        data: {}
    """
    event: Optional[str] = None
    data_parts: list[str] = []

    async def _dispatch() -> None:
        nonlocal event, data_parts
        ev, event = event, None
        raw = "\n".join(data_parts).strip()
        data_parts = []

        if not ev or not raw:
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise APIError(
                f"invalid JSON in Bakbak SSE event '{ev}': {exc}",
                body=raw,
                retryable=False,
            ) from exc

        if ev == "chunk":
            b64 = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(b64, str):
                logger.debug("SSE 'chunk' event missing 'data' string field; skipping")
                return
            try:
                f32 = base64.b64decode(b64)
            except Exception as exc:
                raise APIError(
                    f"invalid base64 in SSE chunk: {exc}", retryable=False
                ) from exc
            output_emitter.push(_f32le_to_s16le(f32))

        elif ev == "done":
            pass  # stream finished cleanly

        else:
            logger.debug("unknown Bakbak SSE event '%s'; ignoring", ev)

    while True:
        line_b = await resp.content.readline()
        if not line_b:
            break
        line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")

        if line == "":
            await _dispatch()
        elif line.startswith("event:"):
            # A new event field means the previous event (if any) is now complete
            await _dispatch()
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].lstrip())
        elif data_parts:
            # Continuation line for a multi-line data field
            data_parts.append(line)

    # Dispatch any final event aren't terminated by a blank line
    await _dispatch()

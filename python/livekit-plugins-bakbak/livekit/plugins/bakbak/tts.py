from __future__ import annotations

import array
import asyncio
import base64
import io
import json
import os
import weakref
from dataclasses import dataclass, replace
from typing import Any, Literal

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
from livekit.agents.utils import is_given

from .log import logger

API_KEY_HEADER = "X-API-Key"
DEFAULT_BASE_URL = "https://hub.getraya.app"


def _resolve_base_url(explicit: str | None) -> str:
    if explicit is not None:
        b = explicit.strip().rstrip("/")
        if b:
            return b
    env = os.environ.get("BAKBAK_BASE_URL") or os.environ.get("RAYA_API_BASE_URL")
    if env:
        return env.strip().rstrip("/")
    return DEFAULT_BASE_URL.rstrip("/")

BakbakCodec = Literal["pcm", "wav", "mp3", "mulaw"]
BakbakLanguage = Literal["hi", "mr", "te", "kn", "bn"]
SampleRate = Literal[8000, 16000, 22050, 24000]


def _f32le_to_s16le_pcm(data: bytes) -> bytes:
    if len(data) % 4 != 0:
        raise APIError("invalid PCM F32LE chunk length", retryable=False)
    floats = array.array("f")
    floats.frombytes(data)
    out = array.array("h")
    for x in floats:
        x = max(-1.0, min(1.0, x))
        out.append(int(round(x * 32767.0)))
    return out.tobytes()


async def _read_error_body(resp: aiohttp.ClientResponse) -> tuple[str, object | None]:
    text = await resp.text()
    try:
        body: object = json.loads(text)
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"]), body
    except json.JSONDecodeError:
        pass
    return text or resp.reason, None


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

    def url(self, path: str) -> str:
        base = self.base_url.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    def request_json(self, text: str) -> dict[str, Any]:
        return {
            "text": text,
            "voice_id": self.voice_id,
            "language": self.language,
            "model": self.model,
            "speed": self.speed,
            "sample_rate": self.sample_rate,
            "codec": "wav" if self.rest_codec == "wav" else self.rest_codec,
        }


class TTS(tts.TTS):
    def __init__(
        self,
        *,
        voice_id: str,
        language: BakbakLanguage | str,
        api_key: str | None = None,
        model: str = "standard",
        speed: float = 1.0,
        sample_rate: SampleRate = 24000,
        base_url: str | None = None,
        rest_codec: BakbakCodec = "wav",
        http_session: aiohttp.ClientSession | None = None,
        tokenizer: NotGivenOr[tokenize.SentenceTokenizer] = NOT_GIVEN,
        text_pacing: tts.SentenceStreamPacer | bool = False,
    ) -> None:
        """Bakbak TTS for LiveKit Agents.

        **API key:** pass ``api_key=`` or set ``BAKBAK_API_KEY`` or ``RAYA_API_KEY``.

        **Base URL:** pass ``base_url=``, or set ``BAKBAK_BASE_URL`` / ``RAYA_API_BASE_URL``,
        or omit both to use the production Raya hub (``https://hub.getraya.app``).
        """
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=int(sample_rate),
            num_channels=1,
        )
        key = api_key or os.environ.get("BAKBAK_API_KEY") or os.environ.get("RAYA_API_KEY")
        if not key:
            raise ValueError(
                "Bakbak API key required: pass api_key= or set BAKBAK_API_KEY or RAYA_API_KEY."
            )

        self._opts = _TTSOptions(
            api_key=key,
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
        self._stream_pacer: tts.SentenceStreamPacer | None = None
        if text_pacing is True:
            self._stream_pacer = tts.SentenceStreamPacer()
        elif isinstance(text_pacing, tts.SentenceStreamPacer):
            self._stream_pacer = text_pacing

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Bakbak"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is not None:
            return self._session
        try:
            self._session = utils.http_context.http_session()
        except RuntimeError:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> SynthesizeStream:
        stream = SynthesizeStream(tts=self, conn_options=conn_options)
        self._streams.add(stream)
        return stream

    async def aclose(self) -> None:
        for s in list(self._streams):
            await s.aclose()
        self._streams.clear()
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None


class ChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        import wave

        session = self._tts._ensure_session()
        url = self._opts.url("/v1/text-to-speech")
        payload = self._opts.request_json(self._input_text)
        payload["codec"] = self._opts.rest_codec

        timeout = aiohttp.ClientTimeout(
            total=120,
            sock_connect=self._conn_options.timeout,
            sock_read=self._conn_options.timeout,
        )
        headers = {API_KEY_HEADER: self._opts.api_key, "Content-Type": "application/json"}

        try:
            async with session.post(
                url, json=payload, headers=headers, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    msg, body = await _read_error_body(resp)
                    raise APIStatusError(
                        message=msg,
                        status_code=resp.status,
                        body=body,
                    )
                body = await resp.read()
        except asyncio.TimeoutError:
            raise APITimeoutError() from None
        except aiohttp.ClientError as e:
            raise APIConnectionError() from e

        sample_rate = self._opts.sample_rate
        pcm: bytes

        if self._opts.rest_codec == "wav":
            try:
                with wave.open(io.BytesIO(body), "rb") as wf:
                    channels = wf.getnchannels()
                    sr = wf.getframerate()
                    sw = wf.getsampwidth()
                    if channels != 1:
                        raise APIError(
                            f"expected mono WAV from Bakbak, got {channels} channels",
                            retryable=False,
                        )
                    if sw != 2:
                        raise APIError(
                            f"expected 16-bit PCM in WAV, got sample width {sw}",
                            retryable=False,
                        )
                    if sr != sample_rate:
                        logger.warning(
                            "WAV sample rate %s differs from configured %s; using WAV rate",
                            sr,
                            sample_rate,
                        )
                        sample_rate = sr
                    pcm = wf.readframes(wf.getnframes())
            except wave.Error as e:
                raise APIError(f"invalid WAV from Bakbak: {e}", retryable=False) from e
        else:
            pcm = body
            if self._opts.rest_codec == "pcm" and len(pcm) % 2 != 0:
                raise APIError("PCM body length is not a multiple of 2 (16-bit)", retryable=False)

        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=int(sample_rate),
            num_channels=1,
            mime_type="audio/pcm",
            stream=False,
        )
        chunk_size = 32000
        for i in range(0, len(pcm), chunk_size):
            output_emitter.push(pcm[i : i + chunk_size])
        output_emitter.flush()


class SynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        request_id = utils.shortuuid()
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=self._opts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        segment_id = utils.shortuuid()
        output_emitter.start_segment(segment_id=segment_id)

        sent_stream = self._tts._sentence_tokenizer.stream()
        if self._tts._stream_pacer is not None:
            sent_stream = self._tts._stream_pacer.wrap(
                sent_stream=sent_stream,
                audio_emitter=output_emitter,
            )

        session = self._tts._ensure_session()
        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=self._conn_options.timeout,
            sock_read=self._conn_options.timeout,
        )
        headers = {API_KEY_HEADER: self._opts.api_key, "Content-Type": "application/json"}
        url = self._opts.url("/v1/text-to-speech/stream")

        async def _input_task() -> None:
            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    sent_stream.flush()
                    continue
                sent_stream.push_text(data)
            sent_stream.end_input()

        async def _synth_task() -> None:
            try:
                async for ev in sent_stream:
                    text = (ev.token or "").strip()
                    if not text:
                        continue
                    self._mark_started()
                    await _stream_one_utterance(
                        session=session,
                        url=url,
                        headers=headers,
                        timeout=timeout,
                        payload_base=self._opts.request_json(text),
                        output_emitter=output_emitter,
                    )
            finally:
                await sent_stream.aclose()

        try:
            await asyncio.gather(_input_task(), _synth_task())
        except Exception:
            raise
        finally:
            output_emitter.end_segment()


async def _stream_one_utterance(
    *,
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    timeout: aiohttp.ClientTimeout,
    payload_base: dict[str, Any],
    output_emitter: tts.AudioEmitter,
) -> None:
    payload = {**payload_base, "codec": "wav"}
    try:
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status >= 400:
                msg, body = await _read_error_body(resp)
                raise APIStatusError(message=msg, status_code=resp.status, body=body)
            if resp.content_type and "text/event-stream" not in resp.content_type:
                logger.debug("unexpected stream content-type: %s", resp.content_type)

            await _parse_sse_audio(resp, output_emitter)
    except asyncio.TimeoutError:
        raise APITimeoutError() from None
    except aiohttp.ClientError as e:
        raise APIConnectionError() from e


async def _parse_sse_audio(
    resp: aiohttp.ClientResponse,
    output_emitter: tts.AudioEmitter,
) -> None:
    event_type: str | None = None
    data_lines: list[str] = []
    in_data = False

    async def dispatch() -> None:
        nonlocal event_type, data_lines, in_data
        if not event_type:
            data_lines = []
            in_data = False
            return
        raw = "\n".join(data_lines).strip()
        data_lines = []
        ev = event_type
        event_type = None
        in_data = False
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise APIError(f"invalid Bakbak SSE JSON: {e}", body=raw, retryable=False) from e

        if ev == "chunk" and isinstance(payload, dict):
            b64 = payload.get("data")
            if not b64 or not isinstance(b64, str):
                return
            try:
                f32 = base64.b64decode(b64)
            except (ValueError, TypeError) as e:
                raise APIError(f"invalid base64 audio in SSE: {e}", retryable=False) from e
            pcm = _f32le_to_s16le_pcm(f32)
            output_emitter.push(pcm)
        elif ev == "done":
            return

    while True:
        line_b = await resp.content.readline()
        if not line_b:
            await dispatch()
            break
        line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            await dispatch()
            continue
        if line.startswith("event:"):
            await dispatch()
            event_type = line[6:].strip()
            in_data = False
        elif line.startswith("data:"):
            in_data = True
            rest = line[5:].lstrip()
            data_lines.append(rest)
        elif in_data and event_type:
            data_lines.append(line)

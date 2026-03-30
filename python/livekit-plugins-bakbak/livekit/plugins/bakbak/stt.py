from __future__ import annotations

import asyncio
import base64
import io
import json
import uuid
import wave
import weakref
from dataclasses import dataclass
from typing import Literal, Optional, Union

import aiohttp

from livekit import rtc

from livekit.agents import (
    APIConnectionError,
    APIError,
    APIStatusError,
    stt,
    utils,
)
from livekit.agents.language import LanguageCode
from livekit.agents.types import (
    APIConnectOptions,
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    NotGivenOr,
)
from livekit.agents.utils import AudioBuffer, is_given

from .log import logger
from ._client import (
    API_KEY_HEADER,
    post_with_retry,
    raise_for_status,
    resolve_api_key,
)
from ._urls import (
    DEFAULT_HUB_URL,
    resolve_hub_base_url,
    transcribe_http_url,
    transcribe_ws_url,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = DEFAULT_HUB_URL

BakbakSTTLanguage = Literal[
    "as",
    "bn",
    "brx",
    "doi",
    "en",
    "gu",
    "hi",
    "kn",
    "kok",
    "ks",
    "mai",
    "ml",
    "mni",
    "mr",
    "ne",
    "or",
    "pa",
    "sa",
    "sat",
    "sd",
    "ta",
    "te",
    "ur",
]


def _pcm_s16le_to_wav(pcm: bytes, *, sample_rate: int, num_channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _speech_language(effective: Optional[str]) -> LanguageCode:
    if effective:
        return LanguageCode(effective)
    return LanguageCode("en")


def _parse_transcribe_json(
    data: dict[str, object], *, speech_language: LanguageCode
) -> stt.SpeechEvent:
    if "detail" in data and "transcript" not in data:
        raise APIStatusError(
            message=str(data.get("detail") or "transcription error"),
            status_code=-1,
            body=data,
            retryable=False,
        )
    status = data.get("status")
    transcript = str(data.get("transcript") or "")
    if status == "error":
        raise APIError(
            transcript or "transcription failed",
            body=data,
            retryable=False,
        )
    if status != "success":
        raise APIError(
            f"unexpected transcription status: {status!r}",
            body=data,
            retryable=False,
        )
    return stt.SpeechEvent(
        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
        request_id=str(uuid.uuid4()),
        alternatives=[
            stt.SpeechData(language=speech_language, text=transcript),
        ],
    )


@dataclass
class _STTOptions:
    api_key: str
    base_url: str
    language: Optional[str]
    sample_rate: int


class STT(stt.STT):
    """LiveKit Agents plugin for Bakbak / Raya speech-to-text.

    Batch recognition uses HTTP ``POST /transcribe`` (multipart WAV). Streaming
    uses ``wss://.../transcribe`` with JSON ``audio_base64`` per flushed segment.

    Args:
        language: Optional hub language code (e.g. ``\"hi\"``, ``\"en\"``).
        api_key: Raya API key, or ``BAKBAK_API_KEY`` / ``RAYA_API_KEY``.
        base_url: Hub base URL, or ``BAKBAK_BASE_URL`` / ``RAYA_API_BASE_URL``.
        sample_rate: Input audio sample rate after resampling (default ``16000``).
        http_session: Optional shared ``aiohttp.ClientSession`` (caller owns lifecycle).
    """

    def __init__(
        self,
        *,
        language: Union[BakbakSTTLanguage, str, None] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        sample_rate: int = 16000,
        http_session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=False,
                offline_recognize=True,
            )
        )
        self._opts = _STTOptions(
            api_key=resolve_api_key(api_key),
            base_url=resolve_hub_base_url(
                base_url, "BAKBAK_BASE_URL", "RAYA_API_BASE_URL"
            ),
            language=str(language) if language else None,
            sample_rate=sample_rate,
        )
        self._session = http_session
        self._own_session = False
        self._streams: weakref.WeakSet[BakbakRecognizeStream] = weakref.WeakSet()

    @property
    def model(self) -> str:
        return "bakbak"

    @property
    def provider(self) -> str:
        return "raya"

    def ensure_session(self) -> aiohttp.ClientSession:
        if self._session is not None:
            return self._session
        try:
            self._session = utils.http_context.http_session()
        except RuntimeError:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    def _effective_language(self, language: NotGivenOr[str]) -> Optional[str]:
        if is_given(language):
            return str(language) if language else None
        return self._opts.language

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        combined = rtc.combine_audio_frames(buffer)
        if combined.num_channels != 1:
            raise APIError(
                f"Bakbak STT expects mono audio, got {combined.num_channels} channels",
                retryable=False,
            )
        wav_bytes = combined.to_wav_bytes()
        eff_lang = self._effective_language(language)
        url = transcribe_http_url(self._opts.base_url)
        headers = {API_KEY_HEADER: self._opts.api_key}
        form = aiohttp.FormData()
        form.add_field(
            "file",
            wav_bytes,
            filename="audio.wav",
            content_type="audio/wav",
        )
        if eff_lang:
            form.add_field("language", eff_lang)

        async with await post_with_retry(
            self.ensure_session(),
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=120,
                sock_connect=conn_options.timeout,
            ),
            log_prefix="Bakbak STT",
            data=form,
        ) as resp:
            await raise_for_status(resp)
            data = await resp.json()
            if not isinstance(data, dict):
                raise APIError(
                    "invalid JSON from Bakbak STT",
                    body=data,
                    retryable=False,
                )
            return _parse_transcribe_json(
                data,
                speech_language=_speech_language(eff_lang),
            )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> BakbakRecognizeStream:
        s = BakbakRecognizeStream(
            stt=self,
            conn_options=conn_options,
            stream_language=self._effective_language(language),
            sample_rate=self._opts.sample_rate,
        )
        self._streams.add(s)
        return s

    async def aclose(self) -> None:
        if self._streams:
            await asyncio.gather(
                *(s.aclose() for s in list(self._streams)), return_exceptions=True
            )
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None


class BakbakRecognizeStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: STT,
        conn_options: APIConnectOptions,
        stream_language: Optional[str],
        sample_rate: int,
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=sample_rate)
        self._stt_ref: STT = stt
        self._stream_language = stream_language
        self._stream_sample_rate = sample_rate

    async def _read_transcribe_response(
        self, ws: aiohttp.ClientWebSocketResponse
    ) -> stt.SpeechEvent:
        while True:
            msg = await ws.receive()
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                raise APIConnectionError("Bakbak STT WebSocket closed unexpectedly")
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise APIConnectionError("Bakbak STT WebSocket error")
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                raw = json.loads(msg.data)
            except json.JSONDecodeError as exc:
                raise APIError(
                    f"invalid JSON from Bakbak STT: {msg.data!r}",
                    retryable=False,
                ) from exc
            if not isinstance(raw, dict):
                continue
            if "transcript" in raw or "detail" in raw:
                return _parse_transcribe_json(
                    raw,
                    speech_language=_speech_language(self._stream_language),
                )
            logger.debug("Bakbak STT ignoring WS message: %s", raw)

        raise APIConnectionError(
            "Bakbak STT WebSocket ended without a transcription response"
        )

    async def _run(self) -> None:
        opts = self._stt_ref._opts
        ws_url = transcribe_ws_url(opts.base_url)
        session = self._stt_ref.ensure_session()
        headers = {API_KEY_HEADER: opts.api_key}

        ws = await session.ws_connect(
            ws_url,
            headers=headers,
            heartbeat=30.0,
        )
        pcm_buf = bytearray()
        try:
            async for item in self._input_ch:
                if isinstance(item, rtc.AudioFrame):
                    if item.num_channels != 1:
                        raise APIError(
                            f"Bakbak STT expects mono audio, got {item.num_channels} channels",
                            retryable=False,
                        )
                    pcm_buf.extend(item.data.tobytes())
                elif isinstance(item, self._FlushSentinel):
                    if not pcm_buf:
                        continue
                    duration_sec = len(pcm_buf) / (2 * self._stream_sample_rate)
                    wav_bytes = _pcm_s16le_to_wav(
                        bytes(pcm_buf),
                        sample_rate=self._stream_sample_rate,
                        num_channels=1,
                    )
                    pcm_buf.clear()
                    req_id = str(uuid.uuid4())
                    payload: dict[str, str] = {
                        "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
                    }
                    if self._stream_language:
                        payload["language"] = self._stream_language

                    await ws.send_str(json.dumps(payload))
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.RECOGNITION_USAGE,
                            request_id=req_id,
                            recognition_usage=stt.RecognitionUsage(
                                audio_duration=duration_sec
                            ),
                        )
                    )
                    ev = await self._read_transcribe_response(ws)
                    ev = stt.SpeechEvent(
                        type=ev.type,
                        request_id=req_id,
                        alternatives=ev.alternatives,
                        recognition_usage=ev.recognition_usage,
                    )
                    self._event_ch.send_nowait(ev)
        finally:
            await ws.close()

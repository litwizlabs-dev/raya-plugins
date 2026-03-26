"""Integration-style tests for Bakbak ``TTS`` (mocked HTTP, no API key required)."""

from __future__ import annotations

import base64
import io
import json
import struct
import wave
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from livekit.agents import APIStatusError

from livekit.plugins.bakbak.tts import DEFAULT_BASE_URL, TTS


def _mono_wav_s16le(*, sample_rate: int = 24000, frames: int = 8) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def _make_post_response(body: bytes, *, status: int = 200) -> MagicMock:
    """Match ``async with await _post_with_retry(...) as resp`` usage."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.release = AsyncMock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_get_response(data: Any, *, status: int = 200) -> tuple[MagicMock, MagicMock]:
    """Return (context_manager, response_mock) for session.get()."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    if status >= 400:
        resp.text = AsyncMock(return_value=json.dumps({"detail": "error"}))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, resp


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.delenv("BAKBAK_API_KEY", raising=False)
    monkeypatch.delenv("RAYA_API_KEY", raising=False)
    return "test-api-key"


@pytest.fixture
def tts_engine(api_key: str) -> TTS:
    return TTS(
        voice_id="voice_1",
        language="hi",
        api_key=api_key,
        base_url="https://test.example",
    )


@pytest.mark.asyncio
async def test_list_voices_accepts_top_level_list(
    tts_engine: TTS, monkeypatch: pytest.MonkeyPatch
) -> None:
    voices = [{"id": "a", "language": "hi"}]
    ctx, _ = _make_get_response(voices)

    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    monkeypatch.setattr(tts_engine, "_session", session)
    monkeypatch.setattr(tts_engine, "_own_session", False)

    out = await tts_engine.list_voices(force_refresh=True)
    assert out == voices
    session.get.assert_called_once()
    url = session.get.call_args[0][0]
    assert url.endswith("/v1/voices")


@pytest.mark.asyncio
async def test_list_voices_accepts_voices_object(
    tts_engine: TTS, monkeypatch: pytest.MonkeyPatch
) -> None:
    voices = [{"id": "b"}]
    ctx, _ = _make_get_response({"voices": voices})

    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    monkeypatch.setattr(tts_engine, "_session", session)
    monkeypatch.setattr(tts_engine, "_own_session", False)

    out = await tts_engine.list_voices(force_refresh=True)
    assert out == voices


@pytest.mark.asyncio
async def test_list_voices_cache_second_call_skips_http(
    tts_engine: TTS, monkeypatch: pytest.MonkeyPatch
) -> None:
    voices = [{"id": "cached"}]
    ctx, _ = _make_get_response(voices)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    monkeypatch.setattr(tts_engine, "_session", session)
    monkeypatch.setattr(tts_engine, "_own_session", False)

    await tts_engine.list_voices(force_refresh=True)
    await tts_engine.list_voices()
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_list_voices_force_refresh_bypasses_cache(
    tts_engine: TTS, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = _make_get_response([{"id": "1"}])
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    monkeypatch.setattr(tts_engine, "_session", session)
    monkeypatch.setattr(tts_engine, "_own_session", False)

    await tts_engine.list_voices(force_refresh=True)
    await tts_engine.list_voices(force_refresh=True)
    assert session.get.call_count == 2


@pytest.mark.asyncio
async def test_list_voices_http_429_raises_api_status(
    tts_engine: TTS, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = _make_get_response([], status=429)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    monkeypatch.setattr(tts_engine, "_session", session)
    monkeypatch.setattr(tts_engine, "_own_session", False)

    with pytest.raises(APIStatusError) as ei:
        await tts_engine.list_voices(force_refresh=True)
    assert ei.value.status_code == 429


@pytest.mark.asyncio
async def test_synthesize_wav_collects_frame_and_emits_metrics(
    tts_engine: TTS, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = _mono_wav_s16le()
    post_resp = _make_post_response(wav)

    async def fake_post(*a: Any, **kw: Any) -> MagicMock:
        return post_resp

    monkeypatch.setattr("livekit.plugins.bakbak.tts._post_with_retry", fake_post)
    metrics: list[Any] = []
    tts_engine.on("metrics_collected", lambda m: metrics.append(m))

    async with tts_engine.synthesize("Hello there") as stream:
        frame = await stream.collect()

    assert frame.sample_rate == 24000
    assert frame.num_channels == 1
    assert len(frame.data) > 0
    non_streamed = [m for m in metrics if not m.streamed]
    assert any(m.characters_count == len("Hello there") for m in non_streamed)
    post_resp.read.assert_awaited()
    await tts_engine.aclose()


@pytest.mark.asyncio
async def test_stream_pushes_audio_and_metrics(
    tts_engine: TTS, monkeypatch: pytest.MonkeyPatch
) -> None:
    b64 = base64.b64encode(struct.pack("<f", 0.0)).decode("ascii")
    lines = [
        b"event: chunk\n",
        b"data: " + json.dumps({"type": "chunk", "data": b64}).encode() + b"\n",
        b"\n",
        b"event: done\n",
        b"data: " + json.dumps({"type": "done", "done": True}).encode() + b"\n",
        b"\n",
    ]

    class FakeContent:
        def __init__(self, data: list[bytes]) -> None:
            self._lines = list(data)
            self._i = 0

        async def readline(self) -> bytes:
            if self._i >= len(self._lines):
                return b""
            line = self._lines[self._i]
            self._i += 1
            return line if line.endswith(b"\n") else line + b"\n"

    stream_resp = MagicMock()
    stream_resp.status = 200
    stream_resp.content_type = "text/event-stream"
    stream_resp.content = FakeContent(lines)
    stream_resp.release = AsyncMock()
    stream_resp.__aenter__ = AsyncMock(return_value=stream_resp)
    stream_resp.__aexit__ = AsyncMock(return_value=None)

    async def fake_post(*a: Any, **kw: Any) -> MagicMock:
        return stream_resp

    monkeypatch.setattr("livekit.plugins.bakbak.tts._post_with_retry", fake_post)
    metrics: list[Any] = []
    tts_engine.on("metrics_collected", lambda m: metrics.append(m))

    async with tts_engine.stream() as stream:
        stream.push_text("One sentence here.")
        stream.end_input()
        frames = [ev.frame async for ev in stream]

    assert len(frames) >= 1
    streamed = [m for m in metrics if m.streamed]
    assert len(streamed) >= 1
    await tts_engine.aclose()


def test_tts_invalid_speed_raises() -> None:
    with pytest.raises(ValueError, match="speed"):
        TTS(voice_id="v", language="hi", api_key="k", speed=2.0)


def test_tts_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKBAK_API_KEY", raising=False)
    monkeypatch.delenv("RAYA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        TTS(voice_id="v", language="hi")


def test_update_options_partial(tts_engine: TTS) -> None:
    assert tts_engine.options.voice_id == "voice_1"
    tts_engine.update_options(voice_id="voice_2", language="mr")
    assert tts_engine.options.voice_id == "voice_2"
    assert tts_engine.options.language == "mr"


def test_update_options_invalid_speed(tts_engine: TTS) -> None:
    with pytest.raises(ValueError, match="speed"):
        tts_engine.update_options(speed=0.1)


def test_provider_and_model(tts_engine: TTS) -> None:
    assert tts_engine.provider == "Bakbak"
    assert tts_engine.model == "standard"


def test_request_json_reflects_options(tts_engine: TTS) -> None:
    j = tts_engine.options.request_json("text", codec="pcm")
    assert j["text"] == "text"
    assert j["voice_id"] == "voice_1"
    assert j["language"] == "hi"
    assert j["codec"] == "pcm"
    assert j["sample_rate"] == 24000


@pytest.mark.asyncio
async def test_aclose_closes_owned_session(
    api_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no http_session is injected, aclose closes the created session."""
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()

    def fake_http_session() -> MagicMock:
        raise RuntimeError("no http context")

    monkeypatch.setattr(
        "livekit.plugins.bakbak.tts.utils.http_context.http_session",
        fake_http_session,
    )

    eng = TTS(voice_id="v", language="hi", api_key=api_key, http_session=None)
    # Replace the ClientSession aiohttp would create with our mock
    eng._session = session
    eng._own_session = True
    await eng.aclose()
    session.close.assert_awaited()


@pytest.mark.asyncio
async def test_resolve_base_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BAKBAK_BASE_URL", raising=False)
    monkeypatch.delenv("RAYA_API_BASE_URL", raising=False)
    eng = TTS(voice_id="v", language="hi", api_key="k", base_url=None)
    assert eng.options.base_url == DEFAULT_BASE_URL.rstrip("/")
    await eng.aclose()

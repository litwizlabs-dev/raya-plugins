"""Tests for Bakbak ``STT`` (mocked HTTP / WebSocket, no API key required)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from livekit import rtc

from livekit.agents import APIError, APIStatusError, stt as stt_module
from livekit.agents.types import APIConnectOptions

from livekit.plugins.bakbak.stt import STT


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.delenv("BAKBAK_API_KEY", raising=False)
    monkeypatch.delenv("RAYA_API_KEY", raising=False)
    return "test-api-key"


@pytest.fixture
def stt_engine(api_key: str) -> STT:
    return STT(
        language="hi",
        api_key=api_key,
        base_url="https://test.example",
        sample_rate=16000,
    )


def _recognize_response_mock(**kwargs: Any) -> MagicMock:
    """Response mock for ``post_with_retry``: awaitable ``post()`` + ``async with resp``."""
    resp = MagicMock()
    resp.release = AsyncMock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    for k, v in kwargs.items():
        setattr(resp, k, v)
    return resp


@pytest.mark.asyncio
async def test_recognize_success(stt_engine: STT, monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _recognize_response_mock(
        status=200,
        json=AsyncMock(return_value={"transcript": "नमस्ते", "status": "success"}),
    )

    session = MagicMock()
    session.post = AsyncMock(return_value=resp)
    monkeypatch.setattr(stt_engine, "ensure_session", lambda: session)

    frame = rtc.AudioFrame(b"\x00\x00" * 160, 16000, 1, 160)
    ev = await stt_engine.recognize(
        [frame], conn_options=APIConnectOptions(max_retry=0)
    )

    assert ev.type == stt_module.SpeechEventType.FINAL_TRANSCRIPT
    assert len(ev.alternatives) == 1
    assert ev.alternatives[0].text == "नमस्ते"
    assert str(ev.alternatives[0].language) == "hi"

    session.post.assert_called_once()
    call_kw = session.post.call_args[1]
    assert "data" in call_kw
    assert call_kw["headers"] == {"X-API-Key": "test-api-key"}


@pytest.mark.asyncio
async def test_recognize_http_error(stt_engine: STT, monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _recognize_response_mock(
        status=401,
        text=AsyncMock(return_value=json.dumps({"detail": "bad key"})),
    )

    session = MagicMock()
    session.post = AsyncMock(return_value=resp)
    monkeypatch.setattr(stt_engine, "ensure_session", lambda: session)

    frame = rtc.AudioFrame(b"\x00\x00" * 80, 16000, 1, 80)
    with pytest.raises(APIStatusError, match="bad key"):
        await stt_engine.recognize(
            [frame], conn_options=APIConnectOptions(max_retry=0)
        )


@pytest.mark.asyncio
async def test_recognize_rejects_stereo(stt_engine: STT) -> None:
    frame = rtc.AudioFrame(b"\x00\x00" * 320, 16000, 2, 80)
    with pytest.raises(APIError, match="mono"):
        await stt_engine.recognize(
            [frame], conn_options=APIConnectOptions(max_retry=0)
        )


@pytest.mark.asyncio
async def test_stream_flush_emits_final(
    stt_engine: STT, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = MagicMock()
    ws.send_str = AsyncMock()
    ws.close = AsyncMock()

    async def recv_side_effect() -> Any:
        return MagicMock(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps({"transcript": "hello", "status": "success"}),
        )

    ws.receive = AsyncMock(side_effect=recv_side_effect)

    async def fake_ws_connect(*args: Any, **kwargs: Any) -> MagicMock:
        return ws

    session = MagicMock()
    session.ws_connect = MagicMock(side_effect=fake_ws_connect)
    monkeypatch.setattr(stt_engine, "ensure_session", lambda: session)

    stream = stt_engine.stream()
    frame = rtc.AudioFrame(b"\x01\x00" * 160, 16000, 1, 160)
    try:
        stream.push_frame(frame)
        stream.flush()
        stream.end_input()

        types: list[stt_module.SpeechEventType] = []
        texts: list[str] = []
        async for ev in stream:
            types.append(ev.type)
            if ev.alternatives:
                texts.append(ev.alternatives[0].text)

        assert stt_module.SpeechEventType.RECOGNITION_USAGE in types
        assert stt_module.SpeechEventType.FINAL_TRANSCRIPT in types
        assert "hello" in texts
    finally:
        await stream.aclose()

    ws.send_str.assert_called_once()
    sent = json.loads(ws.send_str.call_args[0][0])
    assert "audio_base64" in sent
    assert sent.get("language") == "hi"
    ws.close.assert_called()


@pytest.mark.asyncio
async def test_stream_empty_flush_skips_request(
    stt_engine: STT, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = MagicMock()
    ws.send_str = AsyncMock()
    ws.close = AsyncMock()
    ws.receive = AsyncMock()

    async def fake_ws_connect(*args: Any, **kwargs: Any) -> MagicMock:
        return ws

    session = MagicMock()
    session.ws_connect = MagicMock(side_effect=fake_ws_connect)
    monkeypatch.setattr(stt_engine, "ensure_session", lambda: session)

    stream = stt_engine.stream()
    try:
        stream.flush()
        stream.end_input()
        events: list[stt_module.SpeechEvent] = []
        async for ev in stream:
            events.append(ev)
        assert events == []
    finally:
        await stream.aclose()

    ws.send_str.assert_not_called()

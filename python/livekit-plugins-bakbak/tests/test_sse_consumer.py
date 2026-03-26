"""Tests for Bakbak streaming SSE parsing (``_consume_sse_readline``)."""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock

import pytest

from livekit.agents import APIError

from livekit.plugins.bakbak.tts import _consume_sse_readline, _f32le_to_s16le


def _b64_f32_zero() -> str:
    return base64.b64encode(struct.pack("<f", 0.0)).decode("ascii")


def _async_readline_from_lines(lines: list[bytes]) -> Callable[[], Awaitable[bytes]]:
    it = iter(lines)

    async def readline() -> bytes:
        try:
            line = next(it)
        except StopIteration:
            return b""
        return line if line.endswith(b"\n") else line + b"\n"

    return readline


@pytest.mark.asyncio
async def test_sse_style_a_multiline_data_line() -> None:
    """OpenAPI-style ``data:`` on one line and JSON on the next."""
    b64 = _b64_f32_zero()
    chunk_json = {
        "type": "chunk",
        "status_code": 206,
        "done": False,
        "data": b64,
        "step_time": 0.01,
    }
    lines = [
        b"event: chunk\n",
        b"data:\n",
        json.dumps(chunk_json).encode() + b"\n",
        b"\n",
        b"event: done\n",
        b"data: " + json.dumps({"type": "done", "status_code": 200, "done": True}).encode() + b"\n",
        b"\n",
    ]
    emitter = MagicMock()
    await _consume_sse_readline(_async_readline_from_lines(lines), emitter)
    emitter.push.assert_called_once()
    got = emitter.push.call_args[0][0]
    assert got == _f32le_to_s16le(base64.b64decode(b64))


@pytest.mark.asyncio
async def test_sse_style_b_type_in_json_only() -> None:
    b64 = _b64_f32_zero()
    lines = [
        b"data: "
        + json.dumps({"type": "chunk", "data": b64}).encode()
        + b"\n",
        b"\n",
        b"data: " + json.dumps({"type": "done", "done": True}).encode() + b"\n",
        b"\n",
    ]
    emitter = MagicMock()
    await _consume_sse_readline(_async_readline_from_lines(lines), emitter)
    emitter.push.assert_called_once()


@pytest.mark.asyncio
async def test_sse_skips_comment_colon_ping() -> None:
    b64 = _b64_f32_zero()
    lines = [
        b"event: chunk\n",
        b": ping\n",
        b"data: " + json.dumps({"type": "chunk", "data": b64}).encode() + b"\n",
        b"\n",
        b"event: done\n",
        b"data: " + json.dumps({"type": "done", "done": True}).encode() + b"\n",
        b"\n",
    ]
    emitter = MagicMock()
    await _consume_sse_readline(_async_readline_from_lines(lines), emitter)
    emitter.push.assert_called_once()


@pytest.mark.asyncio
async def test_sse_error_event_raises() -> None:
    lines = [
        b"event: error\n",
        b"data: " + json.dumps({"type": "error", "detail": "synthesis failed"}).encode() + b"\n",
        b"\n",
    ]
    emitter = MagicMock()
    with pytest.raises(APIError, match="synthesis failed"):
        await _consume_sse_readline(_async_readline_from_lines(lines), emitter)
    emitter.push.assert_not_called()


@pytest.mark.asyncio
async def test_sse_unknown_event_ignored() -> None:
    b64 = _b64_f32_zero()
    lines = [
        b"event: meta\n",
        b"data: " + json.dumps({"type": "meta", "foo": 1}).encode() + b"\n",
        b"\n",
        b"event: chunk\n",
        b"data: " + json.dumps({"type": "chunk", "data": b64}).encode() + b"\n",
        b"\n",
    ]
    emitter = MagicMock()
    await _consume_sse_readline(_async_readline_from_lines(lines), emitter)
    emitter.push.assert_called_once()

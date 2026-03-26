#!/usr/bin/env python3
"""Hit the real Bakbak API (non-streaming + streaming). Requires network + API key.

Usage (from ``python/livekit-plugins-bakbak``):

    set -a && source .env && set +a
    python scripts/smoke_tts.py

Voice IDs are **not** the doc placeholder ``voice_001`` — they come from your hub.
List them with ``--list-voices``, or omit ``--voice`` to use the **first** voice returned
by ``GET /v1/voices``.

Optional env: ``BAKBAK_VOICE_ID``, ``BAKBAK_LANGUAGE``, ``BAKBAK_BASE_URL`` / ``RAYA_API_BASE_URL``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import aiohttp


def _resolve_api_key() -> str:
    for c in (os.environ.get("BAKBAK_API_KEY"), os.environ.get("RAYA_API_KEY")):
        if c and c.strip():
            return c.strip()
    return ""


def _resolve_base_url() -> str:
    for c in (
        os.environ.get("BAKBAK_BASE_URL"),
        os.environ.get("RAYA_API_BASE_URL"),
    ):
        if c and c.strip():
            return c.strip().rstrip("/")
    return "https://hub.getraya.app".rstrip("/")


async def _fetch_voices(session: aiohttp.ClientSession, base_url: str, api_key: str) -> list[dict]:
    url = f"{base_url}/v1/voices"
    headers = {"X-API-Key": api_key}
    async with session.get(url, headers=headers) as resp:
        text = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"GET /v1/voices {resp.status}: {text[:500]}")
        data = json.loads(text)
    voices = data.get("voices")
    if not isinstance(voices, list):
        raise RuntimeError(f"unexpected /v1/voices response: {text[:500]}")
    return voices


async def main() -> int:
    api_key = _resolve_api_key()
    if not api_key:
        print(
            "Missing API key. Set BAKBAK_API_KEY or RAYA_API_KEY, or: "
            "set -a && source .env && set +a",
            file=sys.stderr,
        )
        return 1

    base_url = _resolve_base_url()
    parser = argparse.ArgumentParser(description="Bakbak TTS smoke test")
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="Print voices from GET /v1/voices and exit",
    )
    parser.add_argument(
        "--voice",
        default=None,
        metavar="ID",
        help="voice_id (default: env BAKBAK_VOICE_ID, else first voice from /v1/voices)",
    )
    parser.add_argument(
        "--language",
        default=None,
        metavar="CODE",
        help="language code (default: env BAKBAK_LANGUAGE, else language of chosen voice, else hi)",
    )
    args = parser.parse_args()

    async with aiohttp.ClientSession() as http:
        if args.list_voices:
            try:
                voices = await _fetch_voices(http, base_url, api_key)
            except Exception as e:
                print(e, file=sys.stderr)
                return 1
            print(json.dumps({"voices": voices, "count": len(voices)}, indent=2))
            return 0

        voice_id = (args.voice or os.environ.get("BAKBAK_VOICE_ID") or "").strip()
        language = args.language or os.environ.get("BAKBAK_LANGUAGE")
        language = language.strip() if isinstance(language, str) and language.strip() else None

        if not voice_id:
            try:
                voices = await _fetch_voices(http, base_url, api_key)
            except Exception as e:
                print(e, file=sys.stderr)
                return 1
            if not voices:
                print(
                    "No voices returned. Check API key and hub URL.",
                    file=sys.stderr,
                )
                return 1
            first = voices[0]
            voice_id = str(first.get("id", "")).strip()
            if not voice_id:
                print("First voice entry has no id.", file=sys.stderr)
                return 1
            if not language:
                language = str(first.get("language", "hi") or "hi")
            print(
                f"Using voice_id={voice_id!r} language={language!r} "
                f"(first of {len(voices)} from GET /v1/voices). "
                f"Run with --list-voices to see all."
            )
        elif not language:
            language = "hi"

    from livekit.plugins.bakbak import TTS

    engine = TTS(voice_id=voice_id, language=language)
    text = "Hello from the Bakbak smoke test."

    try:
        print("Non-streaming (synthesize)…")
        async with engine.synthesize(text) as stream:
            frame = await stream.collect()
        print(
            f"  ok: {frame.samples_per_channel} samples/channel, "
            f"{frame.sample_rate} Hz, {frame.num_channels} ch"
        )

        print("Streaming (SSE)…")
        async with engine.stream() as stream:
            stream.push_text(text)
            stream.end_input()
            n = 0
            async for _ in stream:
                n += 1
        print(f"  ok: {n} audio event(s)")

    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        await engine.aclose()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

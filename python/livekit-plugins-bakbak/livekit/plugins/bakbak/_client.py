"""Shared Bakbak hub HTTP helpers (auth, errors, POST retries)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import aiohttp

from livekit.agents import APIConnectionError, APIStatusError, APITimeoutError

from .log import logger

API_KEY_HEADER = "X-API-Key"

POST_MAX_RETRIES = 3
POST_RETRY_BASE_DELAY = 1.0


def resolve_api_key(explicit: Optional[str]) -> str:
    candidates = [
        explicit,
        os.environ.get("BAKBAK_API_KEY"),
        os.environ.get("RAYA_API_KEY"),
    ]
    value = next((v.strip() for v in candidates if v and v.strip()), None)
    if not value:
        raise ValueError(
            "Bakbak API key required: pass api_key= or set BAKBAK_API_KEY / RAYA_API_KEY."
        )
    return value


async def raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """Read the error body and raise a typed ``APIStatusError``."""
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
    raise APIStatusError(
        message=text or resp.reason, status_code=resp.status, body=detail
    )


async def post_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    timeout: aiohttp.ClientTimeout,
    log_prefix: str = "Bakbak hub",
    **post_kwargs: Any,
) -> aiohttp.ClientResponse:
    """``POST`` with exponential backoff on 429 and transient 5xx / connection errors.

    Extra keyword arguments are forwarded to ``session.post`` (e.g. ``json=...`` or
    ``data=...``). Returns a response suitable for ``async with`` (same as awaiting
    ``session.post`` in aiohttp).
    """
    last_exc: Exception | None = None
    for attempt in range(POST_MAX_RETRIES):
        try:
            resp = await session.post(
                url, headers=headers, timeout=timeout, **post_kwargs
            )
            if resp.status in (429, 500, 502, 503, 504) and attempt < POST_MAX_RETRIES - 1:
                await resp.release()
                delay = POST_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "%s POST %d; retrying in %.1fs (attempt %d/%d)",
                    log_prefix,
                    resp.status,
                    delay,
                    attempt + 1,
                    POST_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue
            return resp
        except asyncio.TimeoutError:
            raise APITimeoutError() from None
        except aiohttp.ClientError as exc:
            last_exc = exc
            if attempt < POST_MAX_RETRIES - 1:
                delay = POST_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "%s connection error: %s; retrying in %.1fs (attempt %d/%d)",
                    log_prefix,
                    exc,
                    delay,
                    attempt + 1,
                    POST_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
            continue
    raise APIConnectionError() from last_exc

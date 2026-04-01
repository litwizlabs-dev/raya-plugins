"""Shared Bakbak hub HTTP helpers and LiveKit metrics labels.

Constants:

    * ``API_KEY_HEADER`` — header name for the Raya API key (``X-API-Key``).
    * ``BAKBAK_METRICS_PROVIDER`` / ``BAKBAK_METRICS_MODEL_STT`` — telemetry labels
      for ``model_provider`` / ``model_name`` in STT (and shared provider for TTS).
    * ``POST_MAX_RETRIES`` / ``POST_RETRY_BASE_DELAY`` — retry policy for hub POSTs.

Functions:

    * ``resolve_api_key`` — resolve key from argument or environment.
    * ``raise_for_status`` — map error responses to ``APIStatusError``.
    * ``post_with_retry`` — POST with backoff on rate limits and transient failures.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import aiohttp

from livekit.agents import APIConnectionError, APIStatusError, APITimeoutError

from .log import logger

API_KEY_HEADER = "X-API-Key"

# LiveKit metrics: ``model_provider`` / ``model_name`` (STT, TTS)
BAKBAK_METRICS_PROVIDER = "raya"
BAKBAK_METRICS_MODEL_STT = "bakbak"

POST_MAX_RETRIES = 3
POST_RETRY_BASE_DELAY = 1.0


def resolve_api_key(explicit: Optional[str]) -> str:
    """Return the Bakbak / Raya API key from the constructor or environment.

    Resolution order:

        #. Non-empty ``explicit`` argument.
        #. ``BAKBAK_API_KEY``
        #. ``RAYA_API_KEY``

    Returns:
        Stripped API key string.

    Raises:
        ValueError: If no non-empty value is found.
    """
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
    return str(value)


async def raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """Raise :class:`livekit.agents.APIStatusError` if the response status is ``>= 400``.

    Parses JSON bodies and prefers a string ``detail`` field for the error message
    when present. Status ``429`` uses a dedicated rate-limit message.

    Args:
        resp: Completed HTTP response from aiohttp.

    Raises:
        APIStatusError: On 4xx/5xx responses (including 429 with a specific message).
    """
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
    err_msg = (text or resp.reason or "").strip() or f"HTTP {resp.status}"
    raise APIStatusError(
        message=err_msg, status_code=resp.status, body=detail
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
    """Perform ``POST`` with exponential backoff on 429 and transient 5xx / connection errors.

    Retries up to ``POST_MAX_RETRIES`` times with delay ``POST_RETRY_BASE_DELAY * 2**attempt``.
    Extra keyword arguments (for example ``json=`` or ``data=``) are forwarded to
    :meth:`aiohttp.ClientSession.post`. The returned value is suitable for
    ``async with`` in the same way as awaiting ``session.post`` in aiohttp.

    Args:
        session: Client session used for the request.
        url: Request URL.
        headers: HTTP headers (must include authentication as required by the hub).
        timeout: Per-request timeout configuration.
        log_prefix: Prefix for warning log lines (e.g. ``"Bakbak STT"``).
        **post_kwargs: Additional arguments for ``session.post``.

    Returns:
        The :class:`aiohttp.ClientResponse` from the final successful attempt.

    Raises:
        APITimeoutError: If a request attempt times out.
        APIConnectionError: If all retries are exhausted due to connection errors.
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

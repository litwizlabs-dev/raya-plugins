# Bakbak plugin for LiveKit Agents

Support for text-to-speech with [Bakbak](https://docs.litwizlabs.com/documentation/tts-overview) (Raya). The API offers non-streaming and SSE streaming endpoints; this plugin maps them to `TTS.synthesize()` and `TTS.stream()` respectively — see [Getting Started with Bakbak TTS](https://docs.litwizlabs.com/documentation/tts-getting-started).

See the [Bakbak TTS documentation](https://docs.litwizlabs.com/documentation/tts-overview) and [API reference](https://docs.litwizlabs.com/api-reference/text-to-speech/text-to-speech.md) for more information.

## Installation

```bash
pip install livekit-plugins-bakbak
```

From this monorepo checkout:

```bash
cd python/livekit-plugins-bakbak
pip install -e .
```

## Pre-requisites

You'll need an API key from Raya (Bakbak TTS). It can be set as an environment variable: `BAKBAK_API_KEY`. If that is unset, `RAYA_API_KEY` is used.

The default API host is `https://hub.getraya.app`. To use another deployment, set `BAKBAK_BASE_URL` or `RAYA_API_BASE_URL`, or pass `base_url` to `livekit.plugins.bakbak.TTS`.

## Local testing

Copy [`.env.example`](.env.example) to `.env` in this directory, set `BAKBAK_API_KEY`, then load env vars in your shell (`.env` is gitignored):

```bash
cd python/livekit-plugins-bakbak
cp .env.example .env
# edit .env
set -a && source .env && set +a   # bash/zsh
```

## Run and test

**1. Environment and install**

```bash
cd python/livekit-plugins-bakbak
uv venv .venv -p 3.12    # or python3 -m venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env
# edit .env — set BAKBAK_API_KEY
set -a && source .env && set +a
```

**2. Unit tests** (no API key; checks audio helper logic)

```bash
pytest -q
```

**3. Live API smoke test** (needs `BAKBAK_API_KEY`, network)

Uses [`scripts/smoke_tts.py`](scripts/smoke_tts.py): one `synthesize()` call and one `stream()` call.

Doc examples may use placeholder IDs like `voice_001`; your hub returns real IDs from **`GET /v1/voices`**. List them:

```bash
python scripts/smoke_tts.py --list-voices
```

Run the smoke test (if you omit `--voice`, the script picks the **first** voice from that list):

```bash
python scripts/smoke_tts.py
python scripts/smoke_tts.py --voice YOUR_VOICE_ID --language hi
```

Override with env: `BAKBAK_VOICE_ID`, `BAKBAK_LANGUAGE`.

**4. Use inside a LiveKit agent**

Install the package in the same environment as `livekit-agents`, set `BAKBAK_API_KEY`, then pass `bakbak.TTS(voice_id=..., language=...)` into your agent session / pipeline as your TTS implementation. Run the worker the same way you do for other LiveKit agent projects (see [LiveKit Agents docs](https://docs.livekit.io/agents/)).

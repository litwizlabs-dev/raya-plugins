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

Add **`[dev]`** if you plan to run `pytest` (`uv pip install -e ".[dev]"` or `pip install -e ".[dev]"`).

## Pre-requisites

You'll need an API key from Raya (Bakbak TTS). It can be set as an environment variable: `BAKBAK_API_KEY`. If that is unset, `RAYA_API_KEY` is used.

The default API host is `https://hub.getraya.app`. To use another deployment, set `BAKBAK_BASE_URL` or `RAYA_API_BASE_URL`, or pass `base_url` to `livekit.plugins.bakbak.TTS`.

## Local setup (API key)

Copy [`.env.example`](.env.example) to `.env`, set `BAKBAK_API_KEY`, then load vars in your shell (`.env` is gitignored):

```bash
cd python/livekit-plugins-bakbak
cp .env.example .env
# edit .env
set -a && source .env && set +a   # bash/zsh
```

Optional in `.env`: `BAKBAK_VOICE_ID`, `BAKBAK_LANGUAGE`, `BAKBAK_BASE_URL`.

## Run and test

### 1. Install in a venv

```bash
cd python/livekit-plugins-bakbak
uv venv .venv -p 3.12    # or: python3 -m venv .venv
source .venv/bin/activate
uv pip install -e .      # runtime only
# or, to run pytest too:
uv pip install -e ".[dev]"
```

#### uv: “`VIRTUAL_ENV` … does not match the project environment path `.venv`”

That happens when your shell’s active venv is **not** this package’s `.venv` — for example `VIRTUAL_ENV` points at the **repo root** (`raya-plugins/.venv`) while you run `uv` from `python/livekit-plugins-bakbak`, where uv expects `python/livekit-plugins-bakbak/.venv`.

**Option A — use this folder’s venv (recommended):**

```bash
deactivate   # if some other venv is active
cd python/livekit-plugins-bakbak
source .venv/bin/activate
uv pip install -e ".[dev]"   # or: uv pip install -e .
```

**Option B — keep the venv you already have active** and install into it:

```bash
cd python/livekit-plugins-bakbak
uv pip install -e ".[dev]" --active
```

If you no longer need a root-level `.venv`, delete it so you don’t activate it by mistake.

### 2. Unit tests (no API key)

```bash
pytest -q
```

### 3. Live API smoke test

[`scripts/smoke_tts.py`](scripts/smoke_tts.py) calls `synthesize()` and `stream()` once each. Doc examples may use placeholder IDs like `voice_001`; your hub returns real IDs from **`GET /v1/voices`**.

```bash
python scripts/smoke_tts.py --list-voices          # discover voice_id values
python scripts/smoke_tts.py                       # uses first voice from that list
python scripts/smoke_tts.py --voice YOUR_ID --language hi
```

| Flag | Purpose |
|------|---------|
| `--list-voices` | Print `GET /v1/voices` JSON and exit |
| `--clean` | Delete generated WAVs under the output folder (no API key needed) |
| `--no-save` | Run checks only; do not write WAV files |
| `--output-dir DIR` | Where to save WAVs (default: `scripts/output/`); same path used by `--clean` |
| `--voice` / `--language` | Override (or set `BAKBAK_VOICE_ID` / `BAKBAK_LANGUAGE`) |

**Output audio:** each run writes timestamped files under [`scripts/output/`](scripts/output/) (contents gitignored; [`.gitkeep`](scripts/output/.gitkeep) keeps the folder in git):

- `bakbak-synthesize-*.wav` — non-streaming result  
- `bakbak-stream-*.wav` — streaming result  

```bash
python scripts/smoke_tts.py --clean
```

### 4. Use inside a LiveKit agent

Install the package in the same environment as `livekit-agents`, set `BAKBAK_API_KEY`, then pass `bakbak.TTS(voice_id=..., language=...)` into your agent session / pipeline. Run your worker as for any LiveKit agent project ([LiveKit Agents docs](https://docs.livekit.io/agents/)).

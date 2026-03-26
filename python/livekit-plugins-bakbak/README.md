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

Add **`[dev]`** if you plan to run `pytest` (`pip install -e ".[dev]"` or `uv pip install -e ".[dev]"`).

## Pre-requisites

You'll need an API key from Raya (Bakbak TTS). It can be set as an environment variable: `BAKBAK_API_KEY`. If that is unset, `RAYA_API_KEY` is used.

The default API host is `https://hub.getraya.app`. To use another deployment, set `BAKBAK_BASE_URL` or `RAYA_API_BASE_URL`, or pass `base_url` to `livekit.plugins.bakbak.TTS`.

## Run and test (this package)

Work from **`python/livekit-plugins-bakbak`** so the virtualenv and paths match this project.

### 1. Virtualenv and install

```bash
cd python/livekit-plugins-bakbak
uv venv .venv -p 3.12          # or: python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"
```

Use **this folder’s** `.venv` when you run `uv pip install` here. If a different environment is already active and `uv` warns about `VIRTUAL_ENV`, either activate `.venv` as above or append **`--active`** to install into the current env.

### 2. API key

```bash
cp .env.example .env
# edit .env — set BAKBAK_API_KEY
set -a && source .env && set +a   # bash/zsh
```

Optional in `.env`: `BAKBAK_VOICE_ID`, `BAKBAK_LANGUAGE`, `BAKBAK_SMOKE_TEXT`, `BAKBAK_BASE_URL`.

### 3. Unit tests (no API key)

```bash
pytest -q
```

### 4. Smoke script (real API + WAV output)

[`scripts/smoke_tts.py`](scripts/smoke_tts.py) exercises **`synthesize()`** and **`stream()`** once each and saves **WAV** files under [`scripts/output/`](scripts/output/).

Doc examples may use placeholder IDs like `voice_001`; your hub returns real IDs from **`GET /v1/voices`**.

**Typical flow:**

```bash
# still in python/livekit-plugins-bakbak, venv active, .env sourced
python scripts/smoke_tts.py --list-voices
python scripts/smoke_tts.py
python scripts/smoke_tts.py --voice YOUR_VOICE_ID --language hi -t "Your text here."
```

| Flag | Purpose |
|------|---------|
| `--list-voices` | Print `GET /v1/voices` JSON and exit |
| `--clean` | Delete generated WAVs under the output folder (no API key needed) |
| `--no-save` | Run checks only; do not write WAV files |
| `--output-dir DIR` | Where to save WAVs (default: `scripts/output/`); same path used by `--clean` |
| `--text` / `-t` | Sentence to synthesize (or env `BAKBAK_SMOKE_TEXT`; default short English phrase) |
| `--voice` / `--language` | Override (or set `BAKBAK_VOICE_ID` / `BAKBAK_LANGUAGE`) |

**Output files** (timestamped per run):

- `bakbak-synthesize-*.wav` — non-streaming  
- `bakbak-stream-*.wav` — streaming  

```bash
python scripts/smoke_tts.py --clean
```

### 5. Use inside a LiveKit agent

Install the package in the same environment as `livekit-agents`, set `BAKBAK_API_KEY`, then pass `bakbak.TTS(voice_id=..., language=...)` into your agent session / pipeline. Run your worker as for any LiveKit agent project ([LiveKit Agents docs](https://docs.livekit.io/agents/)).

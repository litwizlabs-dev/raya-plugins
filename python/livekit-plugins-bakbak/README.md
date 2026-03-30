# Bakbak plugin for LiveKit Agents

**[LiveKit Agents](https://docs.livekit.io/agents/)** TTS and STT for [Bakbak / Raya](https://docs.litwizlabs.com/documentation/tts-overview). Use **`TTS.synthesize(text)`** for full utterances or **`TTS.stream()`** for lower-latency synthesis; use **`STT.recognize()`** for batch transcription or **`STT.stream()`** for realtime segments over WebSocket (see [Speech to Text](https://docs.litwizlabs.com/api-reference/speech-to-text/speech-to-text)).

## Installation

In an existing **uv** project (with a `pyproject.toml`):

```bash
uv add livekit-plugins-bakbak
```

From this monorepo checkout (install this package + dev deps into `.venv`):

```bash
cd python/livekit-plugins-bakbak
uv venv
uv sync --extra dev
```

## Using with LiveKit

1. Install next to **`livekit-agents`** in the same environment as your worker.
2. Set **`BAKBAK_API_KEY`** (or **`RAYA_API_KEY`**). Optional: **`BAKBAK_BASE_URL`** / **`RAYA_API_BASE_URL`** if not using the default hub, or pass **`base_url`** into **`TTS`**.
3. Build a **`bakbak.TTS`** instance and pass it into your agent session / voice pipeline like any other LiveKit TTS plugin.

```python
from livekit.plugins import bakbak

tts = bakbak.TTS(voice_id="YOUR_VOICE_ID", language="hi")
# Wire `tts` into AgentSession / your pipeline — see LiveKit Agents docs.
```

Discover **`voice_id`** values with:

```python
voices = await tts.list_voices()              # cached 1h per instance
voices = await tts.list_voices(force_refresh=True)
```

Account setup, languages, and hub details: [Bakbak TTS docs](https://docs.litwizlabs.com/documentation/tts-getting-started).

### Speech-to-text (STT)

Use the same API key and optional base URL as TTS. Pass **`bakbak.STT`** into **`AgentSession`** (or your pipeline) like any other LiveKit STT plugin.

```python
from livekit.plugins import bakbak

stt = bakbak.STT(language="hi")  # optional; hub also accepts omission
# AgentSession(..., stt=stt, ...)
```

- **`recognize()`** — HTTP `POST /transcribe` with a WAV built from an **`AudioBuffer`**.
- **`stream()`** — WebSocket to `wss://<hub>/transcribe`; each **`flush()`** sends one base64 WAV payload and yields a final transcript for that segment. Default input sample rate is **16 kHz** mono (`sample_rate` on **`STT`**); the stream resamples incoming frames to match.

API reference: [Speech to Text (HTTP)](https://docs.litwizlabs.com/api-reference/speech-to-text/speech-to-text) and the WSS section in the same docs tree.

## Run and test (this package)

Work from **`python/livekit-plugins-bakbak`**.

```bash
cd python/livekit-plugins-bakbak
uv venv
uv sync --extra dev
```

### Running tests (no API key)

Unit tests use mocks and do **not** call the hub:

```bash
cd python/livekit-plugins-bakbak
uv run --extra dev pytest tests/ -v
```

Other useful invocations:

```bash
uv run --extra dev pytest tests/ -q
uv run --extra dev pytest tests/test_tts_features.py -v
uv run --extra dev pytest tests/test_stt.py -v
```

### Smoke script (real API)

[`scripts/smoke_tts.py`](scripts/smoke_tts.py) hits the real API and writes WAVs under [`scripts/output/`](scripts/output/). Set a key first:

```bash
cp .env.example .env   # set BAKBAK_API_KEY
set -a && source .env && set +a
```

```bash
cd python/livekit-plugins-bakbak
uv run --extra dev python scripts/smoke_tts.py --list-voices
uv run --extra dev python scripts/smoke_tts.py --voice YOUR_VOICE_ID --language hi -t "Your text."
uv run --extra dev python scripts/smoke_tts.py --clean
```

| Flag | Purpose |
|------|---------|
| `--list-voices` | List voices (JSON) and exit |
| `--clean` | Remove generated WAVs under the output folder |
| `--no-save` | Run checks only; no WAV files |
| `--output-dir DIR` | Output directory (default: `scripts/output/`) |
| `--text` / `-t` | Text to synthesize |
| `--voice` / `--language` | Overrides (or use `BAKBAK_VOICE_ID` / `BAKBAK_LANGUAGE` in `.env`) |

Use **`uv run --extra dev python scripts/smoke_tts.py --help`** for all options (including debugging helpers).

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

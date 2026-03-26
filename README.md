# raya-plugins

Plugins and integrations for LiveKit and other services.

## Layout

| Path | Purpose |
|------|---------|
| [`python/`](python/) | Python packages (one subdirectory per installable project). |
| [`node/`](node/) | Future npm / TypeScript packages. |

### Python: Bakbak TTS for LiveKit

See [`python/livekit-plugins-bakbak/README.md`](python/livekit-plugins-bakbak/README.md). Package layout follows LiveKit’s plugin convention ([example: Tavus](https://github.com/livekit/agents/tree/main/livekit-plugins/livekit-plugins-tavus)): `pyproject.toml` + `livekit/plugins/<name>/` + README with **Installation** and **Pre-requisites**.

Install:

```bash
cd python/livekit-plugins-bakbak
uv venv .venv -p 3.12
source .venv/bin/activate
uv pip install -e .
```

Keep the virtual environment **inside the plugin directory** (as above), not at the repository root. The `.venv` name is gitignored everywhere.

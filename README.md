# oMLX for Hermes

Managed local MLX models in **Hermes Desktop**, without a Hermes fork or core patches.
Requires **Hermes 0.23.0+**, **macOS 15+**, and **Apple Silicon** on the machine running the gateway.

## What it includes

- Install the checksum-pinned, self-contained **oMLX 0.7.2** runtime.
- Turn the private, authenticated loopback server on and off.
- Download public Hugging Face model repositories, with byte progress and cancellation.
- Browse model sizes, validate real generation before choosing a default, and confirm deletions.
- Use the named **`omlx`** provider through Hermes's public provider interface.

No MLX packages are injected into Hermes's Python environment. Viewing the library or provider picker does not start a server or change the active provider. The model-provider half requires in-process plugin execution; it cannot return its live SDK client through strict host-process isolation.

## Install

Before catalog acceptance, install the repository URL:

```sh
hermes plugins install https://github.com/vitaliy-blprnt/hermes-plugin-omlx
hermes plugins enable omlx
```

Once the catalog submission is merged, `hermes plugins install omlx` is also available.
Enable its Desktop half under **Capabilities → Plugins**, then open **oMLX** in plugin settings.
Both the Python backend and Desktop toggle must be enabled. If an already-running gateway has not loaded the new backend, restart it from an external terminal when ongoing work has finished.

1. Choose **Install**.
2. Download an MLX repository by its exact `owner/model` ID.
3. Choose **Turn on**, then **Use** beside a downloaded model.

**Use** runs a short real generation check before saving that profile's default model. It does not rewrite an already-running chat's model. Select `omlx` and the downloaded model in that chat's usual picker when needed.
After explicit enablement, an inference request can restart the owned runtime. **Turn off** disables that behavior. Registration, credential resolution, catalog listing and client construction never start it.

## Downloads and models

Completion counts the whole snapshot, including cached files. **Already downloaded** is the cached baseline; **received** and network rate are separate transfer counters. Buffered file reconstruction is not treated as network speed, and no reconstruction-based ETA is shown.

Cancel reaps the worker before reporting cancellation and keeps hidden partial files for retry. Cancellation is no longer accepted once atomic snapshot publication begins. There is no pause button or automatic cache purge.

Deletion refuses traversal, symlinks, incomplete/unmanaged snapshots, the active default model, and deletion while the owned server is running. No download, activation or uninstall operation deletes a model automatically.

Runtime, models, logs and resumable cache live under the active profile's durable **`plugin-data/omlx/`** directory. Plugin updates do not replace these files. The plugin does not adopt, move or delete models/runtime installations from the earlier core implementation or a separately installed oMLX.

## Security and operational disclosure

- **Network:** setup fetches the pinned DMG from the official `jundot/omlx` GitHub release, verifies its committed SHA-256, and downloads public model files from Hugging Face/CDN endpoints. Normal inference is loopback-only. No telemetry is added.
- **Native commands:** installation invokes macOS `hdiutil` (read-only mount/detach) and `ditto`. Runtime/download operations invoke the private bundle's `omlx-cli` and `omlx-cluster-python`; no shell command interpolation is used. Their `HOME` is private to the plugin so upstream helper scripts are not written into the operator's home.
- **Background processes:** the server is a long-running native process; install/download/activation are bounded background jobs. Model downloads can consume substantial bandwidth/disk; loaded models consume unified memory.
- **Ownership:** the server binds only `127.0.0.1` at a deterministic per-profile port. The plugin checks process birth identity, executable and owner identity before lifecycle operations. Port conflicts and unverifiable ownership fail closed; it never adopts or kills an arbitrary listener.
- **Credentials:** a local-only API key is generated on first start and stored through Hermes's credential API, read with profile-scoped `get_secret`. It is not written into plugin data or returned by the REST API. The optional masked `local_api_key` setting names `HERMES_OMLX_LOCAL_API_KEY`; changing it requires stopping/restarting the server. No external account key is required.
- **Reads/writes:** Hermes's documented APIs read active-profile config, persist an explicitly selected default and store the plugin's local secret. Model/runtime data stays in the plugin data directory; the plugin never reads credential files, browser profiles or another tool's login directly.
- **HTTP:** local credentials are attached only after verifying the owned runtime. The client rejects another profile/origin and disables HTTP proxy inheritance and redirects. A startup failure is raised at request time, outside core's provider-constructor fallback path.

Only public Hermes extension surfaces are used: `ProviderProfile`, `register_provider`, profile-safe plugin storage/config/model-selection helpers, the authenticated plugin backend mount and `@hermes/plugin-sdk`. No core monkeypatching, internal renderer imports, self-updating plugin code or committed executables.

## Development

From a Hermes development environment with pytest available:

```sh
python -m pytest
hermes plugins validate "$PWD" --install-deps
node --check desktop/plugin.js
```

The test fixtures use isolated homes and disposable models/processes, never an operator's library. Native-runtime and rendered-UI checks are separate from mocked tests; see [verification notes](docs/verification.md) for the published version's actual evidence and remaining limits.

## Provenance

The runtime/download behavior is derived from the unmerged managed-oMLX work in NousResearch/hermes-agent PR #147052, closed in favor of this standalone plugin. The original copyright notice is retained in `LICENSE`. oMLX itself is developed by its upstream authors and fetched only during explicit setup; it is not bundled here.

"""Plugin-owned, profile-scoped oMLX runtime. Import and status never start it."""
from __future__ import annotations

import atexit
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import selectors
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import psutil

VERSION = "0.7.2"
ARCHIVE = f"https://github.com/jundot/omlx/releases/download/v{VERSION}/oMLX-{VERSION}-macos15-sequoia.dmg"
SHA256 = "86e50d13ecbdf8531a5e39684619b0b0a609ec06e1c1fab866daa74e0e47b32e"
SECRET_NAME = "HERMES_OMLX_LOCAL_API_KEY"
REPO_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"
_STATE_FIELDS = {"pid", "create_time", "executable", "owner_pid", "owner_create_time", "base_url"}
_OPERATION_HOME: ContextVar[Path | None] = ContextVar("omlx_operation_home", default=None)


def home() -> Path:
    from hermes_constants import get_hermes_home
    active = Path(get_hermes_home()).resolve()
    expected = _OPERATION_HOME.get()
    if expected is not None and expected != active:
        raise RuntimeError("Active Hermes profile changed during the oMLX operation")
    return active


def root() -> Path:
    # plugin_data_dir creates directories; read paths must not call it.
    return home() / "plugin-data" / "omlx"


def _guard_paths() -> None:
    base = home()
    for path in (base / "plugin-data", root(), root() / "models", root() / "runtime", root() / "cache", root() / "home", root() / "data",
                 root() / "runtime" / VERSION, cli().parents[2], cli().parents[1], cli().parent):
        if path.is_symlink() or not path.resolve().is_relative_to(base):
            raise RuntimeError("Refusing a symlinked oMLX storage path")


def models_dir() -> Path:
    return root() / "models"


def cli() -> Path:
    return root() / "runtime" / VERSION / "oMLX.app/Contents/MacOS/omlx-cli"


def base_url() -> str:
    port = 20000 + int(hashlib.sha256(str(home()).encode()).hexdigest()[:8], 16) % 30000
    return f"http://127.0.0.1:{port}/v1"


def supported() -> bool:
    try:
        return platform.system() == "Darwin" and platform.machine() == "arm64" and int(platform.mac_ver()[0].split(".")[0]) >= 15
    except (ValueError, IndexError):
        return False


def require_supported() -> None:
    if not supported():
        raise ValueError("oMLX requires macOS 15 or newer on Apple Silicon (arm64)")


def _read_json(path: Path) -> dict:
    _guard_paths()
    if path.is_symlink():
        raise RuntimeError("Refusing a symlinked oMLX state file")
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    _guard_paths()
    if path.is_symlink():
        raise RuntimeError("Refusing a symlinked oMLX state file")
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def enabled() -> bool:
    return _read_json(root() / "preferences.json").get("enabled") is True


def set_enabled(value: bool) -> None:
    if type(value) is not bool:
        raise ValueError("enabled must be a boolean")
    if value:
        require_supported()
    with operation_lock():
        _write_json(root() / "preferences.json", {"enabled": value})


@contextmanager
def operation_lock(name: str = "operation"):
    """Cross-process, nonblocking mutation locks; no implicit runtime installation."""
    import fcntl
    if name not in {"operation", "download"}:
        raise ValueError("Invalid oMLX lock")
    _guard_paths()
    from plugins.plugin_storage import plugin_data_dir
    storage = plugin_data_dir("omlx")
    if Path(storage).resolve() != root():
        raise RuntimeError("Active Hermes profile changed during operation")
    lock = root() / f"{name}.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another oMLX operation is in progress; try again shortly") from exc
        token = _OPERATION_HOME.set(home())
        try:
            yield
        finally:
            _OPERATION_HOME.reset(token)
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_state() -> dict:
    return {key: value for key, value in _read_json(root() / "server.json").items() if key in _STATE_FIELDS}


def _process(state: dict):
    if state.get("base_url") != base_url():
        return None
    try:
        if (type(state.get("pid")) is not int or type(state.get("owner_pid")) is not int
                or state["pid"] <= 0 or state["owner_pid"] <= 0
                or type(state.get("owner_create_time")) not in (int, float)):
            return None
        proc = psutil.Process(state["pid"])
        if (state["create_time"] != proc.create_time() or not proc.is_running()
                or proc.status() == psutil.STATUS_ZOMBIE):
            return None
        executable = Path(proc.exe()).resolve()
        bundle = cli().parents[3].resolve()
        if (not executable.is_relative_to(bundle) or executable != Path(state["executable"]).resolve()
                or not bundle.is_relative_to((root() / "runtime").resolve())):
            return None
        return proc
    except (KeyError, TypeError, OSError, ValueError, psutil.Error):
        return None


def owned_process():
    """A verified private child may serve other workers of the same profile."""
    _guard_paths()
    return _process(read_state())


def api_key(create: bool = False) -> str | None:
    home()  # Fail closed if a long-running operation lost its profile scope.
    from agent.secret_scope import get_secret
    key = get_secret(SECRET_NAME)
    if key:
        return key
    if not create:
        return None
    from hermes_cli.config import save_env_value_secure
    key = secrets.token_urlsafe(32)
    saved = save_env_value_secure(SECRET_NAME, key)
    if not saved.get("success"):
        raise RuntimeError("Could not save the managed oMLX credential")
    return key


def child_env() -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR", "USER", "LOGNAME", "SHELL"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    cache = root() / "cache"
    env.update(HOME=str(root() / "home"), HF_HOME=str(cache / "huggingface"), HF_HUB_CACHE=str(cache / "huggingface/hub"),
               HF_XET_CACHE=str(cache / "huggingface/xet"), HF_TOKEN_PATH=str(cache / "huggingface/token"),
               HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    return env


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected oMLX redirect")


def request(path: str, payload: dict | None = None, *, timeout: int = 5):
    if path not in {"/models", "/chat/completions"}:
        raise ValueError("Unsupported oMLX request path")
    if owned_process() is None:
        raise RuntimeError("Managed oMLX is not running under this process")
    key = api_key()
    if not key:
        raise RuntimeError("Managed oMLX credential is unavailable")
    headers = {"Authorization": "Bearer " + key}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(base_url() + path, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        if response.url != req.full_url:
            raise RuntimeError("Unexpected oMLX redirect")
        return json.load(response)


def is_configured(config: dict) -> bool:
    model = (config or {}).get("model") or {}
    return isinstance(model, dict) and model.get("provider") == "omlx"


def _snapshot(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir() or path.name.startswith("."):
        return False
    if path.resolve().parent != models_dir().resolve():
        return False
    files = [path / "config.json", *path.glob("*.safetensors")]
    return len(files) > 1 and all(p.is_file() and not p.is_symlink() for p in files)


def local_models() -> list[str]:
    _guard_paths()
    return sorted(p.name for p in models_dir().glob("*") if _snapshot(p))


def delete_model(model_id: str, config: dict) -> None:
    require_supported()
    with operation_lock("download"), operation_lock():
        if not isinstance(model_id, str) or model_id not in local_models():
            raise ValueError("Model is not a managed oMLX snapshot")
        if is_configured(config) and (config.get("model") or {}).get("default") == model_id:
            raise ValueError("Select a different model before deleting the active model")
        if read_state():
            # An unverifiable record must not allow deleting files used by somebody else's child.
            raise RuntimeError("Turn off oMLX before deleting a model")
        path = models_dir() / model_id
        if not _snapshot(path):
            raise ValueError("Model is not a managed oMLX snapshot")
        shutil.rmtree(path)


def status(config: dict) -> dict:
    _guard_paths()
    compatible = supported()
    model = (config or {}).get("model") or {}
    active = model.get("default") if is_configured(config) else None
    installed = cli().is_file()
    return {"supported": compatible, "installed": installed, "running": owned_process() is not None,
            "enabled": enabled(), "model_dir": str(models_dir()), "base_url": base_url(),
            "version": VERSION if installed else None,
            "models": [{"id": mid, "active": mid == active,
                        "size_bytes": sum(p.stat().st_size for p in (models_dir() / mid).glob("*.safetensors"))}
                       for mid in local_models()],
            "error": None if compatible else "oMLX requires macOS 15 or newer on Apple Silicon (arm64)"}


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        # GitHub releases redirect to their signed asset host; never forward credentials or downgrade TLS.
        if target.scheme != "https" or target.hostname != "release-assets.githubusercontent.com" or target.username or target.password:
            raise RuntimeError("Unexpected oMLX release redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def install(progress=None) -> None:
    require_supported()
    with operation_lock():
        if cli().is_file():
            return
        destination = root() / "runtime" / VERSION
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("An incomplete private oMLX runtime already exists; refusing to replace it")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=root()) as temporary:
            stage = Path(temporary)
            archive = stage / "runtime.dmg"
            digest = hashlib.sha256()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _ReleaseRedirect())
            with opener.open(ARCHIVE, timeout=60) as response, archive.open("wb") as output:
                length = response.headers.get("Content-Length")
                total = int(length) if length and length.isdigit() else 0
                done = 0
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, {})
            if digest.hexdigest() != SHA256:
                raise RuntimeError("oMLX release checksum mismatch")
            mount = stage / "mount"
            mount.mkdir()
            env = child_env()
            attached = False
            try:
                subprocess.run(["/usr/bin/hdiutil", "attach", str(archive), "-readonly", "-nobrowse", "-mountpoint", str(mount)],
                               check=True, capture_output=True, timeout=120, env=env)
                attached = True
                candidate = stage / VERSION
                candidate.mkdir()
                app = candidate / "oMLX.app"
                subprocess.run(["/usr/bin/ditto", str(mount / "oMLX.app"), str(app)], check=True,
                               capture_output=True, timeout=300, env=env)
                for name in ("omlx-cli", "omlx-cluster-python"):
                    executable = app / "Contents/MacOS" / name
                    if not executable.is_file() or not executable.resolve().is_relative_to(app.resolve()):
                        raise RuntimeError("oMLX release is missing its private executable")
                subprocess.run([str(app / "Contents/MacOS/omlx-cli"), "--version"], check=True,
                               capture_output=True, timeout=60, env=env)
            finally:
                if attached or os.path.ismount(mount):
                    subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=True,
                                   capture_output=True, timeout=60, env=env)
            _guard_paths()
            candidate.rename(destination)


def _stop_process(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except psutil.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def start() -> None:
    require_supported()
    if not enabled():
        raise RuntimeError("Explicitly enable managed oMLX before starting it")
    if not cli().is_file():
        raise RuntimeError("Install the managed oMLX runtime first")
    with operation_lock():
        state = read_state()
        proc = _process(state)
        if proc is not None:
            request("/models")
            return
        if state:
            try:
                recorded = psutil.Process(state["pid"])
                alive = recorded.is_running() and recorded.create_time() == state["create_time"]
            except (KeyError, TypeError, psutil.NoSuchProcess):
                alive = False
            if alive:
                raise RuntimeError("A different or unverifiable process owns the recorded oMLX server")
        port = urllib.parse.urlsplit(base_url()).port
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Port {port} is occupied by an unowned server") from exc
        models_dir().mkdir(parents=True, exist_ok=True)
        key = api_key(create=True)
        cmd = [str(cli()), "serve", "--host", "127.0.0.1", "--port", str(port),
               "--base-path", str(root() / "data"), "--model-dir", str(models_dir()), "--api-key", key]
        log_path = root() / "server.log"
        log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(log_fd, "ab") as log:
            child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, env=child_env())
        try:
            owner = psutil.Process()
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f"oMLX exited ({child.returncode}); see {log_path}")
                proc = psutil.Process(child.pid)  # Recreate after the shell wrapper execs Python.
                if Path(proc.exe()).resolve().is_relative_to(cli().parents[3].resolve()):
                    state = {"pid": child.pid, "create_time": proc.create_time(), "executable": proc.exe(),
                             "owner_pid": owner.pid, "owner_create_time": owner.create_time(), "base_url": base_url()}
                    _write_json(root() / "server.json", state)
                    try:
                        request("/models")
                        context = copy_context()
                        atexit.register(context.run, shutdown_owned)
                        return
                    except (OSError, ValueError, RuntimeError):
                        pass
                time.sleep(0.25)
            raise RuntimeError(f"oMLX did not become ready; see {log_path}")
        except BaseException:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
            (root() / "server.json").unlink(missing_ok=True)
            raise


def stop() -> None:
    require_supported()
    with operation_lock():
        state = read_state()
        proc = _process(state)
        if proc is None:
            if state:
                raise RuntimeError("oMLX ownership could not be verified; refusing to stop it")
            return
        current = psutil.Process()
        if state["owner_pid"] != current.pid or state["owner_create_time"] != current.create_time():
            try:
                owner = psutil.Process(state["owner_pid"])
                alive = owner.is_running() and owner.create_time() == state["owner_create_time"]
            except psutil.NoSuchProcess:
                alive = False
            if alive:
                raise RuntimeError("Another live Hermes process owns oMLX; stop it from that process")
        _stop_process(proc)
        (root() / "server.json").unlink(missing_ok=True)


def shutdown_owned() -> None:
    try:
        if owned_process() is not None:
            stop()
    except (OSError, RuntimeError, ValueError, psutil.Error):
        pass


def validate_model(model_id: str) -> None:
    if model_id not in local_models():
        raise ValueError("Model is not downloaded in the managed oMLX model directory")
    start()
    if model_id not in {m.get("id") for m in request("/models").get("data", [])}:
        stop()
        start()
    response = request("/chat/completions", {"model": model_id, "messages": [
        {"role": "user", "content": "Reply with OK."}], "max_tokens": 8, "stream": False}, timeout=300)
    if not response.get("choices") or not any(c.get("message", {}).get("content")
                                               or c.get("message", {}).get("reasoning_content")
                                               for c in response["choices"]):
        raise RuntimeError("oMLX generation validation returned no text; model was not selected")


def _report_download_line(line: bytes, progress, transfer_progress) -> None:
    if line.startswith(b"HERMES_OMLX_PROGRESS "):
        done, total = json.loads(line.removeprefix(b"HERMES_OMLX_PROGRESS "))
        if type(done) is not int or type(total) is not int or not 0 <= done <= total:
            raise RuntimeError("Invalid oMLX download byte counts")
        if progress is not None:
            progress(done, total, {})
    elif line.startswith(b"HERMES_OMLX_TRANSFER "):
        received = json.loads(line.removeprefix(b"HERMES_OMLX_TRANSFER "))
        if type(received) is not int or received < 0:
            raise RuntimeError("Invalid oMLX transfer byte count")
        if transfer_progress is not None:
            transfer_progress(received)


def download(repo: str, *, progress=None, transfer_progress=None, cancel_event=None, before_publish=None) -> str:
    require_supported()
    if not isinstance(repo, str) or not re.fullmatch(REPO_PATTERN, repo):
        raise ValueError("Use a Hugging Face owner/model repository ID")
    python = cli().with_name("omlx-cluster-python")
    if not python.is_file():
        raise RuntimeError("Install the managed oMLX runtime first")
    model_id = repo.replace("/", "--")
    with operation_lock("download"):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Download cancelled")
        stage = models_dir() / ("." + model_id)
        destination = models_dir() / model_id
        if destination.exists() or destination.is_symlink():
            if not _snapshot(destination):
                raise ValueError("Destination is not a managed snapshot")
            if before_publish is not None:
                before_publish()
            return model_id
        if stage.is_symlink():
            raise ValueError("Refusing a symlinked partial snapshot")
        stage.mkdir(parents=True, exist_ok=True)
        if any(path.is_symlink() for path in stage.rglob("*")):
            raise ValueError("Refusing symlinks in a partial snapshot")
        script = Path(__file__).with_name("omlx_download.py")
        args = [str(python), str(script), repo, str(stage)]
        with tempfile.TemporaryFile(dir=stage) as errors, subprocess.Popen(
            args, env=child_env(), stdout=subprocess.PIPE, stderr=errors,
        ) as child:
            assert child.stdout is not None
            deadline = time.monotonic() + 14400
            pending = b""
            try:
                with selectors.DefaultSelector() as reader:
                    reader.register(child.stdout, selectors.EVENT_READ)
                    while reader.get_map():
                        if cancel_event is not None and cancel_event.is_set():
                            raise CancelledError("Download cancelled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(args, 14400)
                        for key, _ in reader.select(min(0.25, remaining)):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                reader.unregister(key.fd)
                                continue
                            lines = (pending + chunk).split(b"\n")
                            pending = lines.pop()
                            if len(pending) > 65536:
                                raise RuntimeError("Invalid oMLX download progress output")
                            for line in lines:
                                _report_download_line(line, progress, transfer_progress)
                if pending:
                    _report_download_line(pending, progress, transfer_progress)
                while child.poll() is None:
                    if cancel_event is not None and cancel_event.wait(0.1):
                        raise CancelledError("Download cancelled")
                    if time.monotonic() >= deadline:
                        raise RuntimeError("oMLX download timed out")
                    time.sleep(0.1)
                child.wait(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("Download cancelled")
            if child.returncode:
                # Public-only worker has no credentials; do not leak remote response bodies in API errors.
                raise RuntimeError(f"Public Hugging Face download failed (exit {child.returncode})")
        if (not (stage / "config.json").is_file() or not any(stage.glob("*.safetensors"))
                or any(path.is_symlink() for path in stage.rglob("*"))):
            raise ValueError("Repository is not a safetensors MLX model (config.json and weights required)")
        if before_publish is not None:
            before_publish()
        elif cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Download cancelled")
        _guard_paths()
        stage.rename(destination)
    return model_id

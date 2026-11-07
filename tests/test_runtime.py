import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import CancelledError
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
from .conftest import IsolatedHome, load


class RuntimeTests(IsolatedHome):
    def test_readonly_profile_scoped_defaults(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        first = runtime.base_url()
        self.assertFalse(runtime.enabled())
        self.assertEqual(runtime.read_state(), {})
        self.assertEqual(runtime.local_models(), [])
        self.assertFalse(self.home.exists())
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home / "second")}):
            self.assertNotEqual(first, runtime.base_url())
        self.assertEqual(first, runtime.base_url())

    def test_mutation_locks_fail_closed_and_guard_profile_changes(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        with runtime.operation_lock():
            with self.assertRaisesRegex(RuntimeError, "operation is in progress"):
                with runtime.operation_lock():
                    pass
            with runtime.operation_lock("download"):
                self.assertEqual(runtime.home(), self.home)
            with patch.dict(os.environ, {"HERMES_HOME": str(self.home / "other")}):
                with self.assertRaisesRegex(RuntimeError, "profile changed"):
                    runtime.api_key(create=True)
        self.assertFalse((self.home / "other").exists())

    def test_process_identity_requires_private_exe_and_birth(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        owner = SimpleNamespace(pid=10, create_time=lambda: 20.0)
        child = SimpleNamespace(pid=11, create_time=lambda: 30.0, is_running=lambda: True,
                                status=lambda: "running", exe=lambda: str(runtime.cli().with_name("python")))
        state = {"pid": 11, "create_time": 30.0, "owner_pid": 10, "owner_create_time": 20.0,
                 "executable": child.exe(), "base_url": runtime.base_url()}
        with patch.object(runtime.psutil, "Process", side_effect=lambda pid=None: owner if pid is None else child):
            self.assertIs(runtime._process(state), child)
            for invalid in ({"owner_pid": None}, {"owner_create_time": None}, {"create_time": 31},
                            {"executable": "/bin/sh"}, {"base_url": "http://127.0.0.1:1/v1"}):
                self.assertIsNone(runtime._process({**state, **invalid}))
            child.exe = lambda: "/bin/sh"
            self.assertIsNone(runtime._process({**state, "executable": "/bin/sh"}))

    def test_serving_peer_can_use_but_not_stop_other_live_owner(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        owner = SimpleNamespace(pid=10, create_time=lambda: 20.0)
        foreign = SimpleNamespace(pid=12, create_time=lambda: 25.0, is_running=lambda: True)
        child = SimpleNamespace(pid=11, create_time=lambda: 30.0, is_running=lambda: True,
                                status=lambda: "running", exe=lambda: str(runtime.cli().with_name("python")))
        state = {"pid": 11, "create_time": 30.0, "owner_pid": 12, "owner_create_time": 25.0,
                 "executable": child.exe(), "base_url": runtime.base_url()}
        processes = {10: owner, 11: child, 12: foreign}
        with patch.object(runtime.psutil, "Process", side_effect=lambda pid=None: processes[10 if pid is None else pid]), patch.object(runtime, "read_state", return_value=state), patch.object(runtime, "require_supported"), patch.object(runtime, "_stop_process") as stop:
            self.assertIs(runtime.owned_process(), child)
            with self.assertRaisesRegex(RuntimeError, "Another live Hermes process"):
                runtime.stop()
            stop.assert_not_called()

    def test_silent_download_cancellation_reaps_real_test_child(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        python = runtime.cli().with_name("omlx-cluster-python")
        python.parent.mkdir(parents=True)
        python.touch()
        cancel, entered = Event(), Event()
        children, errors = [], []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            entered.set()
            return child
        def run():
            try:
                runtime.download("public/model", cancel_event=cancel)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(runtime, "require_supported"), patch.object(runtime.subprocess, "Popen", side_effect=spawn):
            worker = Thread(target=run)
            worker.start()
            self.assertTrue(entered.wait(2))
            cancel.set()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], CancelledError)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue((runtime.models_dir() / ".public--model").is_dir())
        self.assertFalse((runtime.models_dir() / "public--model").exists())

    def test_download_publication_keeps_partial_on_cancel_cutoff(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        python = runtime.cli().with_name("omlx-cluster-python")
        python.parent.mkdir(parents=True)
        python.touch()
        stage = runtime.models_dir() / ".public--model"
        stage.mkdir(parents=True)
        (stage / "config.json").write_text("{}")
        (stage / "weights.safetensors").write_bytes(b"test")
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            return popen([sys.executable, "-c", "print('HERMES_OMLX_PROGRESS [4, 4]')"], **kwargs)
        def cutoff():
            raise CancelledError()
        with patch.object(runtime, "require_supported"), patch.object(runtime.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(CancelledError):
                runtime.download("public/model", before_publish=cutoff)
            self.assertTrue(stage.exists())
            self.assertEqual(runtime.download("public/model"), "public--model")
        self.assertEqual(runtime.local_models(), ["public--model"])

    def test_reject_symlinked_runtime_and_partial(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        elsewhere = Path(self.temporary.name) / "outside"
        elsewhere.mkdir()
        runtime.root().mkdir(parents=True)
        (runtime.root() / "runtime").symlink_to(elsewhere)
        with self.assertRaises(RuntimeError):
            runtime.status({})
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_managed_models_only_and_delete_guards(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        models = runtime.models_dir()
        good = models / "owner--model"
        good.mkdir(parents=True)
        (good / "config.json").write_text("{}")
        (good / "model.safetensors").write_bytes(b"weights")
        (models / "linked").symlink_to(good)
        hidden = models / ".partial"
        hidden.mkdir()
        self.assertEqual(runtime.local_models(), [good.name])
        with patch.object(runtime, "require_supported"):
            for name in ["../owner--model", ".partial", "linked"]:
                with self.assertRaises(ValueError):
                    runtime.delete_model(name, {})
            with self.assertRaises(ValueError):
                runtime.delete_model(good.name, {"model": {"provider": "omlx", "default": good.name}})
            runtime.delete_model(good.name, {})
        self.assertFalse(good.exists())

    def test_secret_and_child_environment_boundaries(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        with patch.dict("os.environ", {"HF_TOKEN": "private", "OPENAI_API_KEY": "private", "HTTP_PROXY": "http://proxy"}):
            env = runtime.child_env()
        self.assertNotIn("HF_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertEqual(env["HF_HUB_DISABLE_IMPLICIT_TOKEN"], "1")
        self.assertEqual(Path(env["HOME"]), runtime.root() / "home")
        self.assertTrue(Path(env["HF_HOME"]).is_relative_to(runtime.root()))
        self.assertFalse(self.home.exists())

    def test_restart_port_probe_accepts_time_wait(self):
        import socket
        runtime = load("omlx_test_runtime", "runtime.py")
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen()
            with socket.create_connection(("127.0.0.1", port)) as client:
                accepted, _ = listener.accept()
                accepted.close()
                self.assertEqual(client.recv(1), b"")
        runtime.cli().parent.mkdir(parents=True)
        runtime.cli().touch()
        with patch.object(runtime, "require_supported"), patch.object(runtime, "enabled", return_value=True), patch.object(runtime, "api_key", return_value="test-key"), patch.object(runtime, "base_url", return_value=f"http://127.0.0.1:{port}/v1"), patch.object(runtime.subprocess, "Popen", side_effect=AssertionError("spawn reached")):
            with self.assertRaisesRegex(AssertionError, "spawn reached"):
                runtime.start()

    def test_state_is_sanitized(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        runtime.root().mkdir(parents=True)
        (runtime.root() / "server.json").write_text(json.dumps({"pid": 123, "api_key": "secret", "base_url": runtime.base_url()}))
        self.assertNotIn("api_key", runtime.read_state())

    def test_installer_checks_digest_before_mount_then_atomically_publishes(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        payload = b"isolated-release-fixture"
        calls = []
        class Release(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}
        def response():
            return Release(payload)
        opener = SimpleNamespace(open=lambda *a, **k: response())
        def run(args, **kwargs):
            calls.append(args)
            if args[0] == "/usr/bin/ditto":
                executable = Path(args[2]) / "Contents/MacOS"
                executable.mkdir(parents=True)
                for name in ("omlx-cli", "omlx-cluster-python"):
                    (executable / name).write_text("fixture")
            return SimpleNamespace(returncode=0)
        with patch.object(runtime, "require_supported"), patch.object(runtime.urllib.request, "build_opener", return_value=opener), patch.object(runtime.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                runtime.install()
            self.assertEqual(calls, [])
            self.assertFalse(runtime.cli().exists())
            with patch.object(runtime, "SHA256", hashlib.sha256(payload).hexdigest()):
                runtime.install()
        self.assertTrue(runtime.cli().is_file())
        self.assertIn("-readonly", calls[0])
        self.assertEqual(calls[-1][1], "detach")
        self.assertEqual(list(runtime.root().glob(".install-*")), [])

    def test_http_auth_refuses_redirects_and_proxies(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        observed = []
        class Reply(io.BytesIO):
            url = runtime.base_url() + "/models"
        def open_request(req, **kwargs):
            observed.append(req)
            return Reply(b'{"data": []}')
        opener = SimpleNamespace(open=open_request)
        with patch.object(runtime, "owned_process", return_value=object()), patch.object(runtime, "api_key", return_value="isolated-nonce"), patch.object(runtime.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(runtime.request("/models"), {"data": []})
            self.assertEqual(observed[0].get_header("Authorization"), "Bearer isolated-nonce")
            self.assertEqual(build.call_args.args[0].proxies, {})
            with self.assertRaises(RuntimeError):
                build.call_args.args[1].redirect_request(None, None, 302, None, None, "https://example.org")
            Reply.url = "https://example.org/models"
            with self.assertRaises(RuntimeError):
                runtime.request("/models")
        self.assertFalse(self.home.exists())

    def test_progress_channels_are_distinct(self):
        runtime = load("omlx_test_runtime", "runtime.py")
        completion, transfer = [], []
        runtime._report_download_line(b'HERMES_OMLX_PROGRESS [4000000000, 8000000000]', lambda *v: completion.append(v), transfer.append)
        runtime._report_download_line(b'HERMES_OMLX_TRANSFER 25', lambda *v: completion.append(v), transfer.append)
        self.assertEqual(completion, [(4000000000, 8000000000, {})])
        self.assertEqual(transfer, [25])
        with self.assertRaises(RuntimeError):
            runtime._report_download_line(b'HERMES_OMLX_PROGRESS [true, 8]', None, None)

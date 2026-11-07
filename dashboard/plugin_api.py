"""Managed oMLX dashboard API, mounted by Hermes at /api/plugins/omlx/."""
from collections import deque
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import copy_context
import importlib.util
from pathlib import Path
from threading import Event, RLock, Thread
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


# Dashboard backends are loaded as synthetic top-level modules, not Python packages.
def _load_runtime():
    spec = importlib.util.spec_from_file_location("omlx_dashboard_runtime", Path(__file__).resolve().parents[1] / "runtime.py")
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load the oMLX plugin runtime")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_reader():
    from hermes_cli.config import load_config_readonly
    return load_config_readonly()


def _selection_writer(*args, **kwargs):
    from hermes_cli.model_setup import persist_model_selection
    return persist_model_selection(*args, **kwargs)


class DownloadBody(BaseModel):
    repo: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=256)


class ModelBody(BaseModel):
    model_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=256)


class CancelBody(BaseModel):
    job_id: str


class Jobs:
    """In-memory, per-profile jobs. A restarted host never claims an old worker is cancellable."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = RLock()
        self.records = {}
        self.busy = {}
        self.events = {}

    def profile(self):
        return str(self.runtime.home())

    def snapshot(self, job_id):
        with self.lock:
            record = self.records.get(job_id)
            if record is None or record["profile"] != self.profile():
                raise HTTPException(404, "Unknown oMLX job")
            return dict(record["job"])

    def listing(self):
        with self.lock:
            profile = self.profile()
            return [dict(record["job"]) for record in self.records.values() if record["profile"] == profile]

    def claim(self, profile, kind):
        active = self.busy.setdefault(profile, set())
        if any({kind, other} not in ({"download", "start"}, {"download", "stop"}) for other in active):
            raise HTTPException(409, "Another oMLX operation is in progress")
        active.add(kind)

    @contextmanager
    def mutation(self, kind):
        profile = self.profile()
        with self.lock:
            self.claim(profile, kind)
        try:
            yield
        finally:
            with self.lock:
                self.busy[profile].discard(kind)

    def cancel(self, job_id):
        with self.lock:
            job = self.snapshot(job_id)
            if not job["can_cancel"] or job["status"] != "running":
                raise HTTPException(409, "This oMLX job can no longer be cancelled")
            event = self.events[job_id]
            event.set()
            self.records[job_id]["job"].update(cancel_requested=True, can_cancel=False, message="Cancelling download")
            return self.snapshot(job_id)

    def spawn(self, kind, action):
        profile = self.profile()
        context = copy_context()
        job_id = uuid.uuid4().hex
        cancel = Event()
        samples = deque()
        with self.lock:
            self.claim(profile, kind)
            job = {"id": job_id, "kind": kind, "status": "running", "phase": kind,
                   "message": f"Starting {kind}", "error": None, "downloaded_bytes": 0,
                   "total_bytes": None, "cached_bytes": None, "received_bytes": 0,
                   "bytes_per_second": None, "can_cancel": kind == "download",
                   "cancel_requested": False, "result": None}
            self.records[job_id] = {"profile": profile, "job": job}
            if kind == "download":
                self.events[job_id] = cancel
            old = [key for key, record in self.records.items() if record["profile"] == profile and record["job"]["status"] != "running"]
            for key in old[:-63]:
                self.records.pop(key)

        def progress(done, total, ranges=None):
            with self.lock:
                if job["cached_bytes"] is None:
                    job["cached_bytes"] = done
                job.update(downloaded_bytes=done, total_bytes=total or None)

        def transfer(received):
            with self.lock:
                now = time.monotonic()
                if samples and received < samples[-1][1]:
                    samples.clear()
                samples.append((now, received))
                while len(samples) > 1 and now - samples[0][0] > 10:
                    samples.popleft()
                elapsed = now - samples[0][0]
                rate = (received - samples[0][1]) / elapsed if len(samples) > 1 and elapsed > 0 else None
                job.update(received_bytes=received, bytes_per_second=rate)

        def before_publish():
            # Cancel and publication share one lock: after this cutoff the rename must finish.
            with self.lock:
                if cancel.is_set():
                    raise CancelledError("Download cancelled")
                job.update(phase="finalizing", message="Finalizing download", can_cancel=False)

        def run():
            try:
                if self.profile() != profile:
                    raise RuntimeError("Active Hermes profile changed before the oMLX job started")
                result = action(progress, transfer, cancel, before_publish)
                with self.lock:
                    job.update(status="complete", phase="complete", message=f"{kind.capitalize()} complete", result=result)
            except CancelledError:
                # runtime.download has already killed and reaped the child before raising.
                with self.lock:
                    job.update(status="cancelled", phase="cancelled", message="Download cancelled")
            except Exception as exc:
                with self.lock:
                    job.update(status="error", phase="error", message=f"{kind.capitalize()} failed", error=str(exc))
            finally:
                with self.lock:
                    job["can_cancel"] = False
                    job["bytes_per_second"] = None
                    self.events.pop(job_id, None)
                    self.busy[profile].discard(kind)
        try:
            Thread(target=context.run, args=(run,), name=f"omlx-{kind}", daemon=True).start()
        except BaseException:
            with self.lock:
                self.records.pop(job_id, None)
                self.events.pop(job_id, None)
                self.busy[profile].discard(kind)
            raise
        return self.snapshot(job_id)


def create_router(runtime=None, *, config_reader=_config_reader, selection_writer=_selection_writer):
    runtime = runtime or _load_runtime()
    jobs = Jobs(runtime)
    api = APIRouter()

    def require_supported():
        if not runtime.supported():
            raise HTTPException(400, "oMLX requires macOS 15 or newer on Apple Silicon (arm64)")

    @contextmanager
    def errors():
        try:
            yield
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (RuntimeError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/status")
    def status():
        with errors():
            return {**runtime.status(config_reader()), "jobs": jobs.listing()}

    @api.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return jobs.snapshot(job_id)

    @api.post("/install")
    def install():
        require_supported()
        return jobs.spawn("install", lambda progress, *_: runtime.install(progress=progress))

    @api.post("/start")
    def start():
        require_supported()
        def action(*_):
            runtime.set_enabled(True)
            runtime.start()
        return jobs.spawn("start", action)

    @api.post("/stop")
    def stop():
        require_supported()
        with jobs.mutation("stop"), errors():
            runtime.set_enabled(False)
            runtime.stop()
        return {"ok": True}

    @api.post("/download")
    def download(body: DownloadBody):
        require_supported()
        return jobs.spawn("download", lambda progress, transfer, cancel, finalizing: runtime.download(
            body.repo, progress=progress, transfer_progress=transfer,
            cancel_event=cancel, before_publish=finalizing))

    @api.post("/cancel")
    def cancel(body: CancelBody):
        return jobs.cancel(body.job_id)

    @api.post("/activate")
    def activate(body: ModelBody):
        require_supported()
        def action(*_):
            runtime.set_enabled(True)
            runtime.validate_model(body.model_id)
            selection_writer(body.model_id, "omlx", base_url="", api_mode="chat_completions")
            return {"model_id": body.model_id, "scope": "profile-default", "live_chat_changed": False}
        return jobs.spawn("activate", action)

    @api.post("/delete")
    def delete(body: ModelBody):
        require_supported()
        with jobs.mutation("delete"), errors():
            runtime.delete_model(body.model_id, config_reader())
        return {"ok": True}

    return api


router = create_router()

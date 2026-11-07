"""REST contract and cancellation races, with an isolated injected runtime."""
import asyncio
from concurrent.futures import CancelledError
from contextvars import ContextVar
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
import httpx

from .conftest import IsolatedHome, load


class JobsTests(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.api = load("hermes_dashboard_plugin_omlx", "dashboard/plugin_api.py")
        self.scope = ContextVar("test_profile", default=str(self.home))
        self.fake = SimpleNamespace(home=lambda: Path(self.scope.get()), supported=lambda: True,
                                    require_supported=lambda: None,
                                    status=lambda config: {"supported": True, "models": []},
                                    set_enabled=Mock(), start=Mock(), stop=Mock(),
                                    validate_model=Mock(), delete_model=Mock())
        self.persist = Mock()
        self.app = FastAPI()
        self.app.include_router(self.api.create_router(self.fake, config_reader=lambda: {}, selection_writer=self.persist))

    def request(self, method, path, body=None):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                return await client.request(method, path, json=body)
        return asyncio.run(run())

    def finished(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.request("GET", f"/jobs/{job_id}").json()
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        self.fail("worker did not finish")

    def test_cancel_stays_running_until_worker_cleanup(self):
        entered, cleanup, release = Event(), Event(), Event()
        def download(repo, **kwargs):
            entered.set()
            kwargs["cancel_event"].wait(3)
            cleanup.set()
            release.wait(3)
            raise CancelledError()
        self.fake.download = download
        job = self.request("POST", "/download", {"repo": "owner/model"}).json()
        self.assertTrue(entered.wait(1))
        response = self.request("POST", "/cancel", {"job_id": job["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(cleanup.wait(1))
        self.assertEqual(self.request("GET", f'/jobs/{job["id"]}').json()["status"], "running")
        release.set()
        self.assertEqual(self.finished(job["id"])["status"], "cancelled")

    def test_finalizing_is_atomic_cancel_cutoff(self):
        finalizing, release = Event(), Event()
        def download(repo, **kwargs):
            kwargs["progress"](4000000000, 8000000000, {})
            kwargs["progress"](6000000000, 8000000000, {})
            kwargs["transfer_progress"](30)
            kwargs["before_publish"]()
            finalizing.set()
            release.wait(3)
            return "owner--model"
        self.fake.download = download
        job = self.request("POST", "/download", {"repo": "owner/model"}).json()
        self.assertTrue(finalizing.wait(1))
        current = self.request("GET", f'/jobs/{job["id"]}').json()
        self.assertEqual(current["cached_bytes"], 4000000000)
        self.assertEqual(current["received_bytes"], 30)
        self.assertIsNone(current["bytes_per_second"])
        self.assertFalse(current["can_cancel"])
        self.assertEqual(self.request("POST", "/cancel", {"job_id": job["id"]}).status_code, 409)
        release.set()
        self.assertEqual(self.finished(job["id"])["status"], "complete")

    def test_context_and_jobs_are_profile_isolated(self):
        self.fake.install = lambda **kwargs: self.assertEqual(self.scope.get(), str(self.home))
        job = self.request("POST", "/install").json()
        self.assertEqual(self.finished(job["id"])["status"], "complete")
        token = self.scope.set(str(self.home / "secondary"))
        try:
            self.assertEqual(self.request("GET", "/status").json()["jobs"], [])
            self.assertEqual(self.request("GET", f'/jobs/{job["id"]}').status_code, 404)
        finally:
            self.scope.reset(token)

    def test_busy_and_activation_public_persistence(self):
        entered, release = Event(), Event()
        def validate(model):
            entered.set()
            release.wait(3)
        self.fake.validate_model = validate
        job = self.request("POST", "/activate", {"model_id": "owner--model"}).json()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.request("POST", "/stop").status_code, 409)
        release.set()
        self.assertEqual(self.finished(job["id"])["status"], "complete")
        self.persist.assert_called_once_with("owner--model", "omlx", base_url="", api_mode="chat_completions")

    def test_failed_validation_does_not_persist(self):
        self.fake.validate_model.side_effect = RuntimeError("no generation")
        job = self.request("POST", "/activate", {"model_id": "owner--model"}).json()
        self.assertEqual(self.finished(job["id"])["status"], "error")
        self.persist.assert_not_called()

    def test_download_does_not_block_runtime_start_or_stop(self):
        entered, release = Event(), Event()
        def download(repo, **kwargs):
            entered.set()
            release.wait(3)
            return "owner--model"
        self.fake.download = download
        job = self.request("POST", "/download", {"repo": "owner/model"}).json()
        try:
            self.assertTrue(entered.wait(1))
            started = self.request("POST", "/start")
            self.assertEqual(started.status_code, 200)
            self.assertEqual(self.finished(started.json()["id"])["status"], "complete")
            self.assertEqual(self.request("POST", "/stop").status_code, 200)
            self.assertEqual(self.request("POST", "/delete", {"model_id": "owner--model"}).status_code, 409)
        finally:
            release.set()
        self.assertEqual(self.finished(job["id"])["status"], "complete")

    def test_api_validates_repo_and_model_paths(self):
        self.assertEqual(self.request("POST", "/download", {"repo": "../bad"}).status_code, 422)
        self.assertEqual(self.request("POST", "/activate", {"model_id": "../bad"}).status_code, 422)

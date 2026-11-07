"""Provider construction, ownership and scope boundaries (no user runtime)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import httpx
from openai import APIConnectionError
import pytest


@pytest.fixture
def provider_module(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    name = "omlx_provider_checks"
    spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load the plugin package from {root}")
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, package)
    spec.loader.exec_module(package)
    module = sys.modules[name + ".provider"]
    monkeypatch.setattr(module, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module.runtime, "base_url", lambda: "http://127.0.0.1:23456/v1")
    yield module


def ready(monkeypatch, module):
    monkeypatch.setattr(module.runtime, "enabled", lambda: True)
    monkeypatch.setattr(module.runtime, "start", lambda: None)
    monkeypatch.setattr(module.runtime, "owned_process", lambda: object())
    monkeypatch.setattr(module.runtime, "api_key", lambda: "test-local-key")


def test_discovery_and_construction_are_inert(provider_module, monkeypatch):
    p = provider_module
    monkeypatch.setattr(p.runtime, "local_models", lambda: ["owner--model"])
    monkeypatch.setattr(p.runtime, "start", lambda: pytest.fail("discovery must not start a server"))
    profile = p.OmlxProfile()
    assert profile.resolve_api_key_secret()[0]
    assert profile.runtime_base_url(api_key="ignored", default="https://old-cloud.example", env_url=None) == "http://127.0.0.1:23456/v1"
    assert profile.fetch_models() == ["owner--model"]
    assert profile.picker_catalog(False) == ["owner--model"]
    client = profile.create_client(api_key="not-used", future_core_option=True)
    assert "client" not in client.__dict__
    client.close()
    assert "client" not in client.__dict__


def test_owned_transport_authenticates_only_verified_loopback(provider_module, monkeypatch):
    p = provider_module
    ready(monkeypatch, p)
    calls = []
    transport = p.OwnedTransport(Path(p.get_hermes_home()), p.runtime.base_url())
    transport.delegate.close()
    transport.delegate = httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, json={"ok": True}))
    request = httpx.Request("POST", p.runtime.base_url() + "/chat/completions")
    assert transport.handle_request(request).status_code == 200
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Bearer test-local-key"
    transport.close()


@pytest.mark.parametrize("case", ["disabled", "unowned", "wrong_origin", "changed_profile"])
def test_transport_refuses_before_sending(provider_module, monkeypatch, tmp_path, case):
    p = provider_module
    ready(monkeypatch, p)
    transport = p.OwnedTransport(Path(p.get_hermes_home()), p.runtime.base_url())
    transport.delegate.close()
    transport.delegate = httpx.MockTransport(lambda req: pytest.fail("must not send any payload"))
    url = p.runtime.base_url() + "/chat/completions"
    if case == "disabled":
        monkeypatch.setattr(p.runtime, "enabled", lambda: False)
    elif case == "unowned":
        monkeypatch.setattr(p.runtime, "owned_process", lambda: None)
    elif case == "wrong_origin":
        url = "https://example.org/v1/chat/completions"
    else:
        monkeypatch.setattr(p, "get_hermes_home", lambda: tmp_path / "different-profile")
    with pytest.raises(httpx.ConnectError):
        transport.handle_request(httpx.Request("POST", url, json={"messages": ["private"]}))
    transport.close()


def test_startup_error_occurs_after_provider_factory_returns(provider_module, monkeypatch):
    p = provider_module
    ready(monkeypatch, p)
    client = p.OmlxProfile().create_client(max_retries=0)
    monkeypatch.setattr(p.runtime, "start", lambda: (_ for _ in ()).throw(RuntimeError("owned startup failed")))
    # Factory returned successfully, so core cannot swallow this as a hook error
    # and silently construct an unguarded generic client.
    with pytest.raises(APIConnectionError):
        client.chat.completions.create(model="fixture", messages=[{"role": "user", "content": "private"}])
    client.close()


def test_sdk_copies_keep_the_ownership_guard(provider_module, monkeypatch):
    p = provider_module
    ready(monkeypatch, p)
    client = p.OmlxProfile().create_client(max_retries=0)
    copied = client.with_options(base_url="https://example.org/v1")
    with pytest.raises(APIConnectionError):
        copied.chat.completions.create(model="fixture", messages=[{"role": "user", "content": "private"}])
    client.close()


def test_sdk_never_follows_redirects(provider_module, monkeypatch):
    p = provider_module
    ready(monkeypatch, p)
    client = p.OmlxProfile().create_client(max_retries=0)
    calls = []
    transport = client.client._client._transport
    transport.delegate.close()
    transport.delegate = httpx.MockTransport(lambda req: calls.append(str(req.url)) or httpx.Response(307, headers={"location": "https://example.org/v1/chat/completions"}))
    try:
        client.chat.completions.create(model="fixture", messages=[{"role": "user", "content": "private"}])
    except Exception:
        pass  # The status error is immaterial: no redirected request may leave.
    assert calls == [p.runtime.base_url() + "/chat/completions"]
    client.close()

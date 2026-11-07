"""Named provider and an owned, loopback-only OpenAI-compatible transport."""
from __future__ import annotations

from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from openai import OpenAI

from hermes_constants import get_hermes_home
from providers.base import ProviderProfile

from . import runtime

# No runtime credentials pass through catalog discovery or a generic HTTP fallback.
_PLACEHOLDER_KEY = "omlx-managed-local-runtime"
_CLIENT_OPTIONS = frozenset({
    "organization", "project", "timeout", "max_retries", "default_headers", "default_query",
})


class OwnedTransport(httpx.BaseTransport):
    """Start only after explicit enablement, and fail before sending to unowned ports."""

    def __init__(self, home: Path, base_url: str):
        self.home = home
        self.origin = urlsplit(base_url)
        self.delegate = httpx.HTTPTransport(retries=0, trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        expected = self.origin
        if (Path(get_hermes_home()) != self.home
                or request.url.scheme != "http"
                or request.url.host != "127.0.0.1"
                or request.url.port != expected.port
                or not request.url.path.startswith("/v1/")):
            raise httpx.ConnectError("oMLX request does not belong to this profile's local runtime", request=request)
        try:
            if not runtime.enabled():
                raise RuntimeError("Turn on oMLX in its plugin settings before using this provider")
            runtime.start()
            if runtime.owned_process() is None:
                raise RuntimeError("Managed oMLX ownership could not be verified")
            key = runtime.api_key()
            if not key:
                raise RuntimeError("Managed oMLX has no profile-scoped local API key")
        except (OSError, ValueError, RuntimeError) as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc
        request.headers["Authorization"] = "Bearer " + key
        return self.delegate.handle_request(request)

    def close(self) -> None:
        self.delegate.close()


class ManagedClient:
    """Defer construction outside core's create_client fallback exception boundary.

    Core catches provider-constructor exceptions and falls back to an ordinary
    client. Returning this inert proxy ensures startup/ownership failures occur
    at request time instead, where they cannot fall through to an unguarded client.
    The OpenAI SDK retains our guarded http_client in with_options/copy clients.
    """

    def __init__(self, options: dict):
        self.options = options
        self.home = Path(get_hermes_home())
        self.base_url = runtime.base_url()

    @cached_property
    def client(self) -> OpenAI:
        return OpenAI(
            **{k: v for k, v in self.options.items() if k in _CLIENT_OPTIONS},
            api_key=_PLACEHOLDER_KEY,
            base_url=self.base_url,
            http_client=httpx.Client(
                transport=OwnedTransport(self.home, self.base_url),
                trust_env=False,
                follow_redirects=False,
            ),
        )

    def __getattr__(self, name):
        return getattr(self.client, name)

    def close(self) -> None:
        if "client" in self.__dict__:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class OmlxProfile(ProviderProfile):
    def __init__(self):
        super().__init__(
            name="omlx",
            display_name="oMLX",
            description="Managed local MLX models on Apple Silicon",
            auth_type="api_key",
            env_vars=("HERMES_OMLX_LOCAL_API_KEY",),
            ambient_credentials=True,
            api_mode="chat_completions",
            # Never claim every loopback endpoint as this provider.
            base_url="http://127.0.0.1:1/v1",
            hostname="omlx.invalid",
            picker_catalog=lambda force_refresh=False: runtime.local_models(),
            supports_health_check=False,
        )

    def resolve_api_key_secret(self):
        # A non-secret sentinel permits discovery before setup; the guarded
        # transport obtains the real Hermes-managed key only for an owned request.
        return _PLACEHOLDER_KEY, "Managed oMLX"

    def runtime_base_url(self, *, api_key=None, default=None, env_url=None):
        return runtime.base_url()

    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0):
        return runtime.local_models()

    def create_client(self, **client_kwargs):
        return ManagedClient(client_kwargs)

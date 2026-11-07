"""Public provider entrypoint; discovery does not install or start oMLX."""
from providers import register_provider

from .provider import OmlxProfile

register_provider(OmlxProfile())

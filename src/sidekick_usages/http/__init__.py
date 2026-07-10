"""Stable Sidekick-owned HTTP facade."""

from sidekick_usages.http.client import HttpClient
from sidekick_usages.http.retry import HttpOperation

__all__ = ["HttpClient", "HttpOperation"]

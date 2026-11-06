"""Exception hierarchy for suno-dl.

Having a small, explicit taxonomy lets callers distinguish expected network
conditions from programming errors instead of collapsing everything into a
generic ``Exception``.
"""

from __future__ import annotations


class SunoError(Exception):
    """Base class for all suno-dl errors."""


class ConfigError(SunoError, ValueError):
    """Invalid configuration or command-line input."""


class ResolutionError(SunoError):
    """A track's media URL could not be resolved from its page."""


class BlockedError(SunoError):
    """The remote refused the request (401/403); retrying will not help."""


class DownloadError(SunoError):
    """A media object could not be downloaded after all candidates failed."""


class FetchError(SunoError, RuntimeError):
    """A page could not be fetched after retries.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` handlers
    (the CLI, tests) keep working; it exists so callers can distinguish an
    exhausted retry budget from unrelated runtime failures.
    """


class DiskSpaceError(SunoError):
    """Insufficient free space on target volume (below --min-free-mb threshold)."""

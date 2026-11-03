"""Errors shared by the collector and optional transport integrations."""


class IntegrationError(RuntimeError):
    """A configured transport could not safely complete its operation."""


class SessionError(IntegrationError):
    """Compatibility name for managed Session transport errors."""

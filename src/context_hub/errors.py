"""Domain errors with stable, user-facing messages."""


class ContextHubError(RuntimeError):
    """Base error for expected Context Hub failures."""


class ValidationError(ContextHubError):
    """The requested operation violates the public contract."""


class NotFoundError(ContextHubError):
    """A stable reference or project could not be found."""


class IndexPendingError(ContextHubError):
    """The fact store is durable but the derived index needs rebuilding."""

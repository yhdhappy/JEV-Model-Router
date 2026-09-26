"""Domain exceptions for JEV Model Router."""


class RouterError(Exception):
    """Base exception for router-domain failures."""


class SchemaValidationError(RouterError):
    """Raised when an external payload cannot satisfy the router schema."""


class ConfigurationError(RouterError):
    """Raised when router configuration is invalid."""


class BudgetExceededError(RouterError):
    """Raised when a request would exceed its configured budget."""

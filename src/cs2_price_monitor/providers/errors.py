from __future__ import annotations


class PriceProviderError(Exception):
    """Base error for external price provider failures."""


class ApiConfigurationError(PriceProviderError):
    """Raised when an external API provider lacks required configuration."""


class ApiHttpError(PriceProviderError):
    """Raised for HTTP transport failures and non-success statuses."""


class ApiDecodeError(PriceProviderError):
    """Raised when an API response cannot be decoded."""


class ApiSchemaNotConfiguredError(PriceProviderError):
    """Raised when no real API schema mapping is configured."""

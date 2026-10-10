class ProviderError(Exception):
    """An inference provider is unavailable or returned an invalid response."""


class DocumentError(ValueError):
    """A document cannot be safely parsed or indexed."""

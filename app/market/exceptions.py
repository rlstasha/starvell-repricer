class StarvellClientError(RuntimeError):
    """Base error for Starvell/Statvell client failures."""


class StarvellWriteDisabledError(StarvellClientError):
    """Raised when a write operation is blocked by the safe client boundary."""


class StarvellEndpointNotConfiguredError(StarvellClientError):
    """Raised when a safe GET endpoint is required but not configured."""


class StarvellPayloadStyleError(StarvellClientError):
    """Raised when the configured price update payload style is not supported."""

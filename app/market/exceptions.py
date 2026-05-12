class StarvellClientError(RuntimeError):
    """Base error for Starvell/Statvell client failures."""


class StarvellNotImplementedError(StarvellClientError):
    """Raised when real Starvell API integration has not been added yet."""


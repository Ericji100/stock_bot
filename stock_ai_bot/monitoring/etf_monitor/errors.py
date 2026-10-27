from typing import Any, Mapping


class EtfMonitorError(RuntimeError):
    """Base error for the isolated ETF monitor."""


class EtfMonitorConfigError(EtfMonitorError):
    """The ETF monitor configuration is unsafe or incomplete."""


class EtfMonitorDataError(EtfMonitorError):
    """Accepted market data is missing, stale, or inconsistent."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.reason_code = reason_code or type(self).__name__.upper()
        self.details = dict(details or {})


class EtfMonitorNextSessionOpenUnavailable(EtfMonitorDataError):
    """The verified raw open for the fixed next trading session is unavailable."""


class EtfMonitorRoutingError(EtfMonitorError):
    """The dedicated Telegram route could not be verified."""


class EtfMonitorStateError(EtfMonitorError):
    """Persistent ETF state is inconsistent."""

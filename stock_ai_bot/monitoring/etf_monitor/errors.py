class EtfMonitorError(RuntimeError):
    """Base error for the isolated ETF monitor."""


class EtfMonitorConfigError(EtfMonitorError):
    """The ETF monitor configuration is unsafe or incomplete."""


class EtfMonitorDataError(EtfMonitorError):
    """Accepted market data is missing, stale, or inconsistent."""


class EtfMonitorNextSessionOpenUnavailable(EtfMonitorDataError):
    """The verified raw open for the fixed next trading session is unavailable."""


class EtfMonitorRoutingError(EtfMonitorError):
    """The dedicated Telegram route could not be verified."""


class EtfMonitorStateError(EtfMonitorError):
    """Persistent ETF state is inconsistent."""

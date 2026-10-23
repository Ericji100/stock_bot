"""Bounded retries for price downloads; failures are never synthetic prices."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import pandas as pd

LOGGER = logging.getLogger(__name__)


def frame_or_empty(value: Any) -> pd.DataFrame:
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def fetch_history_with_retry(
    fetch: Callable[[], Any],
    *,
    normalize: Callable[[pd.DataFrame], pd.DataFrame] = frame_or_empty,
    attempts: int = 2,
    label: str = "行情",
) -> pd.DataFrame:
    """Retry missing/malformed responses without changing provider globals.

    A rate-limit response goes straight to the caller's fallback. Error text
    is intentionally not logged: provider exceptions may contain credentials.
    """
    attempts = max(1, min(int(attempts), 3))
    for attempt in range(attempts):
        rate_limited = False
        try:
            frame = frame_or_empty(normalize(frame_or_empty(fetch())))
            if not frame.empty:
                return frame
        except Exception as exc:
            rate_limited = type(exc).__name__ == "YFRateLimitError" or "429" in str(exc)
        if rate_limited:
            break
        if attempt + 1 < attempts:
            time.sleep(min(2.0, 0.5 * (2 ** attempt)))
    LOGGER.warning("%s：本次未取得可用行情，改查備援；仍缺資料時不計算訊號。", label)
    return pd.DataFrame()


def fetch_fallback_frame(fetch: Callable[[], Any], *, normalize=frame_or_empty) -> pd.DataFrame:
    """The fallback is attempted once and may also fail without crashing a scan."""
    try:
        return frame_or_empty(normalize(frame_or_empty(fetch())))
    except Exception:
        return pd.DataFrame()

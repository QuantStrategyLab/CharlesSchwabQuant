"""Cloud Run request helpers for CharlesSchwabQuant."""

from __future__ import annotations

from datetime import datetime

import pandas_market_calendars as mcal
import pytz


def is_market_open_today(*, calendar_name="NASDAQ", timezone_name="America/New_York") -> bool:
    tz_ny = pytz.timezone(timezone_name)
    now_ny = datetime.now(tz_ny)
    calendar = mcal.get_calendar(calendar_name)
    schedule = calendar.schedule(start_date=now_ny.date(), end_date=now_ny.date())
    return not schedule.empty

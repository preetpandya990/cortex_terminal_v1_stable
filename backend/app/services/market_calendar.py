import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


@dataclass
class TradingSession:
    is_trading_day: bool
    is_open_now: bool
    market_open_utc: datetime | None
    market_close_utc: datetime | None


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(hour=int(hour), minute=int(minute))


class NSECalendarService:
    """
    NSE trading calendar with cached official holidays.
    """

    def __init__(self) -> None:
        self._holidays: set[date] = set()
        self._last_refresh_utc: datetime | None = None
        self._cache_path = Path(settings.NSE_HOLIDAY_CACHE_FILE)
        self._open_time = _parse_hhmm(settings.NSE_MARKET_OPEN_IST)
        self._close_time = _parse_hhmm(settings.NSE_MARKET_CLOSE_IST)
        self._cache_ttl = timedelta(hours=12)

    async def refresh_if_needed(self) -> None:
        now = datetime.now(UTC)
        if self._last_refresh_utc and now - self._last_refresh_utc < self._cache_ttl:
            return

        loaded = await self._fetch_official_holidays()
        if loaded:
            self._last_refresh_utc = now
            return

        # Network/API failure fallback: local cache if available.
        self._load_cached_holidays()
        self._last_refresh_utc = now

    async def _fetch_official_holidays(self) -> bool:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
                response = await client.get(settings.NSE_HOLIDAY_API_URL)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to refresh NSE holidays from official API: %s", exc)
            return False

        holidays = self._extract_holidays(payload)
        if not holidays:
            logger.warning("NSE holiday API returned no usable holiday dates")
            return False

        self._holidays = holidays
        self._write_cache()
        logger.info("Loaded %d NSE holiday dates from official API", len(holidays))
        return True

    @staticmethod
    def _extract_holidays(payload: object) -> set[date]:
        holidays: set[date] = set()
        if not isinstance(payload, dict):
            return holidays

        # NSE payload typically has "FO" and "CM" arrays with "tradingDate".
        for key, value in payload.items():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                raw_date = item.get("tradingDate") or item.get("date")
                if not isinstance(raw_date, str):
                    continue
                parsed = NSECalendarService._parse_nse_date(raw_date)
                if parsed:
                    holidays.add(parsed)
        return holidays

    @staticmethod
    def _parse_nse_date(raw_date: str) -> date | None:
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(raw_date, fmt).date()
            except ValueError:
                continue
        return None

    def _write_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(
                    {
                        "updated_at": datetime.now(UTC).isoformat(),
                        "holidays": sorted(d.isoformat() for d in self._holidays),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist NSE holiday cache: %s", exc)

    def _load_cached_holidays(self) -> None:
        try:
            if not self._cache_path.exists():
                return
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            holidays = payload.get("holidays", [])
            restored = set()
            for value in holidays:
                restored.add(datetime.fromisoformat(value).date())
            if restored:
                self._holidays = restored
                logger.info("Loaded %d NSE holidays from local cache", len(restored))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load NSE holiday cache: %s", exc)

    def get_session(self, at_utc: datetime | None = None) -> TradingSession:
        now_utc = at_utc or datetime.now(UTC)
        now_ist = now_utc.astimezone(IST)
        day = now_ist.date()

        is_weekend = now_ist.weekday() >= 5
        is_holiday = day in self._holidays
        is_trading_day = not is_weekend and not is_holiday

        if not is_trading_day:
            return TradingSession(
                is_trading_day=False,
                is_open_now=False,
                market_open_utc=None,
                market_close_utc=None,
            )

        open_ist = datetime.combine(day, self._open_time, tzinfo=IST)
        close_ist = datetime.combine(day, self._close_time, tzinfo=IST)
        return TradingSession(
            is_trading_day=True,
            is_open_now=open_ist <= now_ist <= close_ist,
            market_open_utc=open_ist.astimezone(UTC),
            market_close_utc=close_ist.astimezone(UTC),
        )

    def is_trading_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return day not in self._holidays

    def get_recent_trading_days(self, count: int, end_day: date | None = None) -> list[date]:
        """
        Return most recent trading dates ending at `end_day` (inclusive if trading day),
        in descending order.
        """
        if count <= 0:
            return []

        cursor = end_day or datetime.now(IST).date()
        output: list[date] = []
        while len(output) < count:
            if self.is_trading_day(cursor):
                output.append(cursor)
            cursor = cursor - timedelta(days=1)
        return output


nse_calendar = NSECalendarService()

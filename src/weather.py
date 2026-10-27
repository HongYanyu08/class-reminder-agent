"""Weather lookup for departure lead times (Open-Meteo, no API key needed).

Two layers, mirroring src/sender.py's structure:

- Pure layer: classify one forecast hour as clear / rain / severe from its
  WMO weather code and precipitation probability, and turn a level into
  extra departure minutes from user preferences. Fully unit-testable, no I/O.
- Provider layer: fetch hourly forecasts from the Open-Meteo forecast API
  via an injectable transport (tests never touch the network), cache the
  parsed result on disk (default TTL 3 h — the scheduler ticks every
  5 minutes and must not hammer the API), and degrade gracefully.

Failure model: weather is an enhancement, never a blocker. No coordinates
configured -> the feature is disabled and level lookups return None
("unavailable"). Fetch failure -> fall back to a stale cache if one exists,
else unavailable. Nothing here raises into the scheduler tick.

Coordinates come from the WEATHER_LAT / WEATHER_LON environment variables
ONLY — never from data files, and no default location is baked in, because
guessing where the user lives would be exactly the kind of fabrication the
rest of the project refuses to do.
"""
import json, os, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

API_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = "weather_code,precipitation_probability"
FORECAST_DAYS = 16  # Open-Meteo free tier horizon; expansion window is 14 days

DEFAULT_CACHE_TTL_H = 3.0
DEFAULT_TIMEOUT = 10  # seconds
DEFAULT_RAIN_PROBABILITY_THRESHOLD = 50  # percent

# WMO weather codes (Open-Meteo "weather_code"). Heavy precipitation and
# thunderstorms are "severe"; any other precipitation is "rain".
SEVERE_CODES = frozenset({
    65,       # heavy rain
    67,       # heavy freezing rain
    75,       # heavy snowfall
    82,       # violent rain showers
    86,       # heavy snow showers
    95, 96, 99,  # thunderstorm (with hail)
})
PRECIP_CODES = frozenset({
    51, 53, 55,        # drizzle
    56, 57,            # freezing drizzle
    61, 63,            # rain (slight / moderate)
    66,                # freezing rain (light)
    71, 73,            # snowfall (slight / moderate)
    77,                # snow grains
    80, 81,            # rain showers (slight / moderate)
    85,                # snow showers (slight)
})

LEVEL_NOTES = {"rain": "降雨，已提前 {extra} 分钟",
               "severe": "恶劣天气，已提前 {extra} 分钟"}


class WeatherError(Exception):
    """A fetch/parse failure. Always caught inside the provider layer."""


# ------------------------------------------------------------- pure layer ----
def classify_hour(weather_code, precipitation_probability,
                  rain_probability_threshold=DEFAULT_RAIN_PROBABILITY_THRESHOLD):
    """Classify one forecast hour: "severe" / "rain" / "clear", or None when
    the entry carries no usable signal at all (both fields missing). A known
    precipitation code decides on its own; for dry codes the precipitation
    probability can still push the hour to "rain" at/above the threshold."""
    if weather_code in SEVERE_CODES:
        return "severe"
    if weather_code in PRECIP_CODES:
        return "rain"
    if (precipitation_probability is not None
            and precipitation_probability >= rain_probability_threshold):
        return "rain"
    if weather_code is not None or precipitation_probability is not None:
        return "clear"
    return None


def extra_minutes(level, prefs):
    """(extra_min, note) for a weather level. clear / unavailable -> (0, None).
    The amounts live in user preferences (rainExtraMin / severeExtraMin)."""
    if level == "rain":
        extra = int(prefs.get("rainExtraMin", 10))
    elif level == "severe":
        extra = int(prefs.get("severeExtraMin", 20))
    else:
        return 0, None
    if extra <= 0:
        return 0, None
    return extra, LEVEL_NOTES[level].format(extra=extra)


def parse_forecast(payload):
    """Normalize an Open-Meteo forecast response into a list of hour dicts
    {"time": "YYYY-MM-DDTHH:MM", "weather_code": int|None,
     "precipitation_probability": number|None}.
    Raises WeatherError on a structurally invalid payload — garbage input
    must degrade to 'unavailable', never be read as 'clear'."""
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise WeatherError("预报响应缺少 hourly.time")
    codes = hourly.get("weather_code") or []
    probs = hourly.get("precipitation_probability") or []
    out = []
    for i, t in enumerate(hourly["time"]):
        out.append({
            "time": t,
            "weather_code": codes[i] if i < len(codes) else None,
            "precipitation_probability": probs[i] if i < len(probs) else None,
        })
    return out


def level_in_hours(hours, when, rain_probability_threshold=DEFAULT_RAIN_PROBABILITY_THRESHOLD):
    """Weather level for the hour containing `when` (a datetime), or None
    when the forecast does not cover that hour (too far out / missing data).
    Forecast times are wall-clock local times in the requested timezone, so
    the comparison is done on the wall-clock hour string."""
    key = when.strftime("%Y-%m-%dT%H:00")
    for entry in hours:
        if entry.get("time") == key:
            return classify_hour(entry.get("weather_code"),
                                 entry.get("precipitation_probability"),
                                 rain_probability_threshold)
    return None


# --------------------------------------------------------- provider layer ----
def _urllib_transport(url, timeout):
    """Default transport: GET via urllib (stdlib only).
    Returns (status_code, body_text). Raises on transport-level errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "class-reminder-agent"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def build_url(lat, lon, timezone="Asia/Shanghai"):
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "hourly": HOURLY_VARS,
        "timezone": timezone, "forecast_days": FORECAST_DAYS,
    })
    return f"{API_URL}?{query}"


class WeatherProvider:
    """Forecast provider with an on-disk cache and graceful degradation.

    status after the last lookup: "ok" (fresh fetch or fresh cache),
    "stale" (fetch failed, expired cache used), "unavailable" (no data),
    "disabled" (no coordinates configured).
    """

    def __init__(self, lat=None, lon=None, cache_path=None, timezone="Asia/Shanghai",
                 ttl_hours=DEFAULT_CACHE_TTL_H, timeout=DEFAULT_TIMEOUT,
                 transport=None, allow_fetch=True):
        self.lat, self.lon = lat, lon
        self.enabled = lat is not None and lon is not None
        self.cache_path = Path(cache_path) if cache_path else None
        self.timezone = timezone
        self.ttl = timedelta(hours=float(ttl_hours))
        self.timeout = timeout
        self.transport = transport or _urllib_transport
        # run_scheduler's --dry-run must not write ANY file, cache included;
        # it passes allow_fetch=False and only a pre-existing cache is read.
        self.allow_fetch = allow_fetch
        self.status = "disabled" if not self.enabled else "unavailable"
        self._forecast = None  # memoized per instance: at most one fetch per tick

    # -- cache ---------------------------------------------------------------
    def _read_cache(self):
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(data["fetchedAt"])
            if not isinstance(data.get("hourly"), list):
                return None
            return fetched, data["hourly"]
        except Exception:
            return None  # a corrupt cache is just no cache

    def _write_cache(self, fetched_at, hours):
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(
                {"fetchedAt": fetched_at.isoformat(), "hourly": hours},
                ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # cache is best-effort; never crash the tick over it

    # -- forecast --------------------------------------------------------------
    def forecast(self, now):
        """(hours, status) for the current tick. Memoized per instance."""
        if self._forecast is not None:
            return self._forecast
        result = ([], "disabled") if not self.enabled else self._load(now)
        self._forecast = result
        self.status = result[1]
        return result

    def _load(self, now):
        cached = self._read_cache()
        if cached and now - cached[0] <= self.ttl:
            return cached[1], "ok"
        if self.allow_fetch:
            try:
                status, body = self.transport(
                    build_url(self.lat, self.lon, self.timezone), self.timeout)
                if not (200 <= int(status) < 300):
                    raise WeatherError(f"Open-Meteo 返回 HTTP {status}")
                hours = parse_forecast(json.loads(body))
            except Exception:
                hours = None
            if hours is not None:
                self._write_cache(now, hours)
                return hours, "ok"
        if cached:
            return cached[1], "stale"
        return [], "unavailable"

    def level_at(self, when, now,
                 rain_probability_threshold=DEFAULT_RAIN_PROBABILITY_THRESHOLD):
        """Weather level at datetime `when`, or None when unavailable
        (disabled, no data, or the hour is outside the forecast horizon)."""
        hours, _ = self.forecast(now)
        if not hours:
            return None
        return level_in_hours(hours, when, rain_probability_threshold)


def _parse_coord(raw, lo, hi):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if lo <= value <= hi else None


def build_weather_provider(env=None, cache_path=None, timezone="Asia/Shanghai",
                           allow_fetch=True, transport=None):
    """Build a provider from the environment (mirrors sender.build_sender).
    WEATHER_LAT / WEATHER_LON are both required; missing or invalid values
    disable the feature rather than guessing a location. Optional:
    WEATHER_CACHE_TTL_H (hours, default 3), WEATHER_TIMEOUT (seconds)."""
    env = os.environ if env is None else env
    lat = _parse_coord(env.get("WEATHER_LAT"), -90, 90)
    lon = _parse_coord(env.get("WEATHER_LON"), -180, 180)
    if lat is None or lon is None:
        return WeatherProvider(cache_path=cache_path, timezone=timezone,
                               allow_fetch=allow_fetch, transport=transport)
    try:
        ttl = float(env.get("WEATHER_CACHE_TTL_H", DEFAULT_CACHE_TTL_H))
    except (TypeError, ValueError):
        ttl = DEFAULT_CACHE_TTL_H
    try:
        timeout = float(env.get("WEATHER_TIMEOUT", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return WeatherProvider(lat=lat, lon=lon, cache_path=cache_path,
                           timezone=timezone, ttl_hours=ttl, timeout=timeout,
                           allow_fetch=allow_fetch, transport=transport)

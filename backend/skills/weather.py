"""
Weather skill — free, keyless current-conditions lookup via Open-Meteo.

Two-step lookup, no API key required:
    1. geocode(location)      — turns a place name into lat/lon.
    2. get_weather(location)  — fetches current conditions for those
                                 coordinates.

Set WEATHER_DEFAULT_LOCATION in backend/.env (e.g. "Lahore, Pakistan")
so "what's the weather" / "weather here" works without naming a city
every time.
"""
import os
import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

DEFAULT_LOCATION = os.getenv("WEATHER_DEFAULT_LOCATION", "")

# WMO weather codes -> short description (covers the common cases).
_WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow",
    80: "light rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    95: "thunderstorm", 96: "thunderstorm with light hail", 99: "thunderstorm with heavy hail",
}


def _describe(code: int) -> str:
    return _WEATHER_CODES.get(code, f"code {code}")


def _nominatim_geocode(location: str) -> dict | None:
    """Fallback for small towns/localities that Open-Meteo's own geocoder
    doesn't know about — Nominatim (OpenStreetMap) has much broader
    coverage of small places, so try it before giving up entirely."""
    try:
        resp = httpx.get(
            NOMINATIM_SEARCH_URL,
            params={"q": location, "format": "jsonv2", "limit": 1},
            headers={"Accept-Language": "en", "User-Agent": "Cipher-personal-assistant/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        return None
    results = resp.json()
    if not results:
        return None
    r = results[0]
    display = r.get("display_name", "")
    name = r.get("name") or (display.split(",")[0] if display else location)
    return {"name": name, "country": "", "lat": float(r["lat"]), "lon": float(r["lon"])}


def geocode(location: str) -> dict | None:
    resp = httpx.get(GEOCODE_URL, params={"name": location, "count": 1}, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("results")
    if results:
        r = results[0]
        return {"name": r["name"], "country": r.get("country", ""), "lat": r["latitude"], "lon": r["longitude"]}
    return _nominatim_geocode(location)


def get_weather(location: str | None = None, lat: float | None = None, lon: float | None = None) -> dict:
    """
    Looks up current conditions. If `lat`/`lon` are given (e.g. from the
    browser's live GPS), those are used directly — no geocoding needed,
    which also sidesteps cases where a small locality's name isn't in
    any geocoder's database even though we already know exactly where
    it is. Otherwise falls back to geocoding `location`, then to
    WEATHER_DEFAULT_LOCATION from .env.
    """
    place_name = None
    if lat is not None and lon is not None:
        plat, plon = lat, lon
        place_name = location or None
    else:
        location = (location or DEFAULT_LOCATION or "").strip()
        if not location:
            return {"error": "No location given, and WEATHER_DEFAULT_LOCATION isn't set in .env."}
        try:
            place = geocode(location)
        except Exception as e:
            return {"error": f"Couldn't look up '{location}': {e}"}
        if place is None:
            return {"error": f"Couldn't find a place called '{location}'."}
        plat, plon = place["lat"], place["lon"]
        place_name = f"{place['name']}, {place['country']}".strip(", ")

    try:
        resp = httpx.get(
            FORECAST_URL,
            params={
                "latitude": plat,
                "longitude": plon,
                "current_weather": "true",
                "temperature_unit": "celsius",
                "windspeed_unit": "kmh",
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        return {"error": f"Couldn't fetch weather: {e}"}

    current = resp.json().get("current_weather", {})
    return {
        "ok": True,
        "location": place_name or "your location",
        "temperature_c": current.get("temperature"),
        "windspeed_kmh": current.get("windspeed"),
        "condition": _describe(current.get("weathercode", -1)),
    }


def get_forecast(location: str | None = None, lat: float | None = None, lon: float | None = None, days: int = 3) -> dict:
    """
    Daily forecast (rain chance, condition, high/low) for the next
    `days` days. Same lat/lon-bypasses-geocoding behavior as
    get_weather() — pass real coordinates when you have them.
    """
    place_name = None
    if lat is not None and lon is not None:
        plat, plon = lat, lon
        place_name = location or None
    else:
        location = (location or DEFAULT_LOCATION or "").strip()
        if not location:
            return {"error": "No location given, and WEATHER_DEFAULT_LOCATION isn't set in .env."}
        try:
            place = geocode(location)
        except Exception as e:
            return {"error": f"Couldn't look up '{location}': {e}"}
        if place is None:
            return {"error": f"Couldn't find a place called '{location}'."}
        plat, plon = place["lat"], place["lon"]
        place_name = f"{place['name']}, {place['country']}".strip(", ")

    try:
        resp = httpx.get(
            FORECAST_URL,
            params={
                "latitude": plat,
                "longitude": plon,
                "daily": "precipitation_probability_max,weathercode,temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
                "forecast_days": max(1, min(days, 7)),
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        return {"error": f"Couldn't fetch forecast: {e}"}

    daily = resp.json().get("daily", {})
    dates = daily.get("time", [])
    rain_chance = daily.get("precipitation_probability_max", [])
    codes = daily.get("weathercode", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])

    if not dates:
        return {"error": "Forecast service returned no data."}

    out_days = []
    for i, date in enumerate(dates):
        out_days.append({
            "date": date,
            "rain_chance_pct": rain_chance[i] if i < len(rain_chance) else None,
            "condition": _describe(codes[i]) if i < len(codes) else None,
            "temp_max_c": tmax[i] if i < len(tmax) else None,
            "temp_min_c": tmin[i] if i < len(tmin) else None,
        })

    return {"ok": True, "location": place_name or "your location", "days": out_days}

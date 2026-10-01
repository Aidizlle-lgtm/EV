"""Weather via Open-Meteo (free, no API key). Location is set in Settings."""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..services import settings, status

WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers", 81: "Rain showers",
    82: "Violent rain showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
}
_cache: dict[str, Any] = {"at": 0.0, "key": None, "data": None}
CACHE_SECONDS = 900


def configured() -> bool:
    return settings.get("weather.lat") is not None and settings.get("weather.lon") is not None


async def geocode(name: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": name, "count": 1, "language": "en", "format": "json"})
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        raise LookupError(f"Couldn't find a place called '{name}'.")
    top = results[0]
    label = ", ".join(x for x in (top.get("name"), top.get("admin1"), top.get("country")) if x)
    return {"label": label, "lat": top["latitude"], "lon": top["longitude"]}


async def set_location(name: str) -> dict[str, Any]:
    place = await geocode(name)
    settings.put("weather.location", name)
    settings.put("weather.label", place["label"])
    settings.put("weather.lat", place["lat"])
    settings.put("weather.lon", place["lon"])
    _cache["at"] = 0.0
    return place


async def current(force: bool = False) -> dict[str, Any]:
    if not configured():
        raise LookupError("Set your location in Settings → Integrations → Weather.")
    lat, lon = settings.get("weather.lat"), settings.get("weather.lon")
    units = settings.get("weather.units") or "celsius"
    key = (lat, lon, units)
    if not force and _cache["key"] == key and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    params = {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1, "temperature_unit": units,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        status.record("weather", False, f"Weather service unreachable: {type(e).__name__}")
        raise
    cur, daily = body.get("current", {}), body.get("daily", {})
    unit = "°C" if units == "celsius" else "°F"
    data = {
        "location": settings.get("weather.label") or settings.get("weather.location"),
        "temperature": cur.get("temperature_2m"), "feels_like": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"), "wind_kmh": cur.get("wind_speed_10m"),
        "condition": WMO.get(cur.get("weather_code"), "Unknown"), "unit": unit,
        "high": (daily.get("temperature_2m_max") or [None])[0], "low": (daily.get("temperature_2m_min") or [None])[0],
        "precip_chance": (daily.get("precipitation_probability_max") or [None])[0],
        "observed_at": cur.get("time"), "source": "Open-Meteo",
    }
    _cache.update(at=time.time(), key=key, data=data)
    status.record("weather", True, f"{data['temperature']}{unit} {data['condition']}")
    return data

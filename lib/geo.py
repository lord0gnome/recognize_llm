"""EXIF GPS extraction + reverse geocoding: location tags and prompt context.

Best-effort throughout — a photo without GPS, an unreadable image, or a geocoder
outage must never fail or noticeably slow a captioning job. Lookups go to a
Nominatim server (public OSM by default, self-hostable via settings), cached
forever in the queue DB on a ~110 m grid and rate-limited to 1 req/s per the
public Nominatim usage policy.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

import httpx
from nc_py_api.ex_app import persistent_storage
from PIL import ExifTags, Image

try:  # HEIC/HEIF (iPhone) — the main carriers of GPS EXIF
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

_DB = os.path.join(persistent_storage(), "recognize_llm_queue.db")
_USER_AGENT = "recognize_llm/0.1 (Nextcloud ExApp)"
_MIN_INTERVAL = 1.1  # public Nominatim policy: at most 1 request/second
_net_lock = threading.Lock()
_last_request = 0.0


@dataclass
class Location:
    lat: float
    lon: float
    landmark: str = ""            # POI name when Nominatim resolves one ("Tour Eiffel")
    parts: list[str] = field(default_factory=list)  # suburb → city → region → country
    city: str = ""
    country: str = ""

    def context(self) -> str:
        """Human-readable line for the vision prompt."""
        names = ", ".join(p for p in ([self.landmark] + self.parts) if p)
        coords = f"GPS {self.lat:.5f}, {self.lon:.5f}"
        return f"{names} ({coords})" if names else coords

    def tags(self) -> list[str]:
        """Deterministic place tags, most specific first."""
        return [t.lower() for t in (self.landmark, self.city, self.country) if t]


def locate(image_bytes: bytes, settings) -> Location | None:
    """GPS coords + place names for an image, or None if it has no usable GPS."""
    if not settings.geotag:
        return None
    coords = _gps_from_exif(image_bytes)
    if coords is None:
        return None
    loc = _lookup(coords[0], coords[1], settings)
    # Geocoder down/unknown area: still return bare coordinates — the model can
    # often place them, and the prompt context remains useful.
    return loc if loc is not None else Location(coords[0], coords[1])


# ── EXIF ──────────────────────────────────────────────────────────────────────

def _dms(value) -> float | None:
    try:
        d, m, s = value
        return float(d) + float(m) / 60 + float(s) / 3600
    except Exception:
        return None


def _gps_from_exif(image_bytes: bytes) -> tuple[float, float] | None:
    try:
        exif = Image.open(io.BytesIO(image_bytes)).getexif()
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if not gps:
            return None
        lat = _dms(gps.get(2))  # GPSLatitude
        lon = _dms(gps.get(4))  # GPSLongitude
        if lat is None or lon is None or (lat == 0 and lon == 0):
            return None
        if str(gps.get(1, "N")).upper().startswith("S"):  # GPSLatitudeRef
            lat = -lat
        if str(gps.get(3, "E")).upper().startswith("W"):  # GPSLongitudeRef
            lon = -lon
        return (lat, lon)
    except Exception:
        return None


# ── Reverse geocoding ─────────────────────────────────────────────────────────

def _cache() -> sqlite3.Connection:
    con = sqlite3.connect(_DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute(
        "CREATE TABLE IF NOT EXISTS geo_cache (key TEXT PRIMARY KEY, payload TEXT, ts INTEGER)"
    )
    return con


def _lookup(lat: float, lon: float, settings) -> Location | None:
    # v2 prefix: payload shape changed when the Overpass landmark lookup was added.
    key = f"v2:{lat:.3f},{lon:.3f}"  # ~110 m grid: photos from the same spot share one lookup
    try:
        with _cache() as con:
            row = con.execute("SELECT payload FROM geo_cache WHERE key=?", (key,)).fetchone()
        if row is not None:
            entry = json.loads(row[0])
        else:
            entry = {
                "nominatim": _nominatim(lat, lon, settings.nominatim_url),
                "landmark": _overpass_landmark(lat, lon, settings.overpass_url),
            }
            with _cache() as con:
                con.execute(
                    "INSERT OR REPLACE INTO geo_cache (key, payload, ts) VALUES (?, ?, ?)",
                    (key, json.dumps(entry), int(time.time())),
                )
        if not entry.get("nominatim") and not entry.get("landmark"):
            return None
        loc = _parse(lat, lon, entry.get("nominatim") or {})
        if entry.get("landmark"):  # Overpass beats Nominatim's nearest-object name
            loc.landmark = entry["landmark"]
        return loc
    except Exception:
        return None  # network/geocoder trouble: caller falls back to bare coordinates


def _throttled(request_fn):
    """Run one outbound geocoding request, ≥ _MIN_INTERVAL after the previous one."""
    global _last_request
    with _net_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            return request_fn()
        finally:
            _last_request = time.monotonic()


def _nominatim(lat: float, lon: float, base_url: str) -> dict:
    if not base_url:
        return {}
    try:
        resp = _throttled(lambda: httpx.get(
            base_url.rstrip("/") + "/reverse",
            params={
                "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                "format": "jsonv2", "zoom": 18,
                "addressdetails": 1, "accept-language": "en",
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        ))
        resp.raise_for_status()
        data = resp.json()
        return {} if "error" in data else data  # "error": e.g. open ocean — cache the miss
    except Exception:
        return {}


# OSM feature values that count as a "known landmark" worth naming/tagging.
_LANDMARK_TOURISM = "attraction|museum|viewpoint|artwork|gallery|zoo|theme_park|aquarium"


def _overpass_landmark(lat: float, lon: float, overpass_url: str) -> str:
    """Name of the closest named tourism/historic feature within 150 m, or "".

    Nominatim reverse snaps to the nearest object of ANY kind (a defibrillator,
    a bench) and photos are taken near landmarks, not inside their footprint —
    an around-radius search is the only reliable way to name the landmark.
    """
    if not overpass_url:
        return ""
    query = (
        f'[out:json][timeout:8];('
        f'nwr(around:150,{lat:.6f},{lon:.6f})[tourism~"^({_LANDMARK_TOURISM})$"][name];'
        f'nwr(around:150,{lat:.6f},{lon:.6f})[historic][name];'
        f');out tags center 10;'
    )
    try:
        resp = _throttled(lambda: httpx.post(
            overpass_url, data={"data": query},
            headers={"User-Agent": _USER_AGENT}, timeout=15,
        ))
        resp.raise_for_status()
        best_name, best_rank = "", None
        for el in resp.json().get("elements", []):
            tags = el.get("tags", {})
            name = tags.get("name:en") or tags.get("name") or ""
            elat = el.get("lat") or (el.get("center") or {}).get("lat")
            elon = el.get("lon") or (el.get("center") or {}).get("lon")
            if not name or elat is None or elon is None:
                continue
            dist = (elat - lat) ** 2 + (elon - lon) ** 2  # fine for ranking at 150 m scale
            # The landmark itself is a large way/relation, usually wikidata-tagged; its
            # nearest sub-feature (an observation deck, a plaque) is a plain node.
            rank = (el.get("type") == "node", "wikidata" not in tags, dist)
            if best_rank is None or rank < best_rank:
                best_name, best_rank = name, rank
        return best_name
    except Exception:
        return ""


def _parse(lat: float, lon: float, data: dict) -> Location:
    addr = data.get("address", {}) or {}
    # Nominatim's "name" is only meaningful as a landmark when the resolved object is
    # actually landmark-shaped — it snaps to the closest object of any kind, so an
    # unfiltered name yields tags like a corner shop or street furniture.
    landmark = str(data.get("name") or "")
    if data.get("category") not in ("tourism", "historic", "leisure", "natural", "man_made"):
        landmark = ""
    city = str(
        addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""
    )
    parts = []
    for k in ("suburb", "neighbourhood", "city_district"):
        if addr.get(k):
            parts.append(str(addr[k]))
            break
    if city:
        parts.append(city)
    if addr.get("state"):
        parts.append(str(addr["state"]))
    country = str(addr.get("country") or "")
    if country:
        parts.append(country)
    return Location(lat, lon, landmark=landmark, parts=parts, city=city, country=country)

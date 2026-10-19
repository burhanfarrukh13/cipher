"""
Nearby-places skill — free, keyless "what's near me" lookups.

Two lookup paths, both free/keyless (OpenStreetMap data):
    1. Overpass (fast, precise, tag-based) for well-known everyday
       categories in _CATEGORY_MAP — restaurant, cafe, pharmacy, etc.
    2. Nominatim search (generic text search, bounded to a box around
       the user) for literally anything else — "government school",
       "mosque", "dentist", "driving school", whatever the user types.
       Less precise than a tag lookup, but works for any noun instead
       of only a fixed whitelist.

extract_nearby_query() also lives here: it pulls the "thing" out of a
free-form question like "is there any government school near me" —
stripping filler words — so main.py doesn't need its own whitelist of
recognized phrasings either.
"""
import math
import re
import difflib
import httpx

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# Maps common everyday words to OSM's actual amenity/shop/leisure tags.
_CATEGORY_MAP = {
    "restaurant": ["amenity=restaurant"],
    "restaurants": ["amenity=restaurant"],
    "food": ["amenity=restaurant", "amenity=fast_food"],
    "eat": ["amenity=restaurant", "amenity=fast_food"],
    "cafe": ["amenity=cafe"],
    "cafes": ["amenity=cafe"],
    "coffee": ["amenity=cafe"],
    "pharmacy": ["amenity=pharmacy"],
    "pharmacies": ["amenity=pharmacy"],
    "chemist": ["amenity=pharmacy"],
    "hospital": ["amenity=hospital"],
    "hospitals": ["amenity=hospital"],
    "atm": ["amenity=atm"],
    "atms": ["amenity=atm"],
    "bank": ["amenity=bank"],
    "banks": ["amenity=bank"],
    "grocery": ["shop=supermarket", "shop=grocery"],
    "groceries": ["shop=supermarket", "shop=grocery"],
    "supermarket": ["shop=supermarket"],
    "supermarkets": ["shop=supermarket"],
    "gas": ["amenity=fuel"],
    "petrol": ["amenity=fuel"],
    "fuel": ["amenity=fuel"],
    "gym": ["leisure=fitness_centre"],
    "gyms": ["leisure=fitness_centre"],
    "park": ["leisure=park"],
    "parks": ["leisure=park"],
    "hotel": ["tourism=hotel"],
    "hotels": ["tourism=hotel"],
    "bakery": ["shop=bakery"],
    "bakeries": ["shop=bakery"],
}

DEFAULT_CATEGORY_KEYS = ["amenity=restaurant"]

# Generic words that often trail the real category in everyday phrasing
# ("grocery shops", "coffee places", "pharmacy stores") but aren't part
# of the actual OSM lookup key. Stripped before matching against
# _CATEGORY_MAP so "grocery shops" -> "grocery" instead of falling
# through to the much less reliable generic text search.
_GENERIC_SUFFIXES = {"shop", "shops", "store", "stores", "place", "places", "spot", "spots"}


def _normalize_category(category: str) -> str:
    words = category.strip().lower().split()
    while len(words) > 1 and words[-1] in _GENERIC_SUFFIXES:
        words.pop()
    normalized = " ".join(words) if words else category.strip().lower()
    if normalized in _CATEGORY_MAP:
        return normalized
    # Tolerate common typos/misspellings ("restraunt", "pharmacie", etc.)
    # by fuzzy-matching against known category keys before giving up on
    # a precise tag lookup and falling through to generic text search.
    close = difflib.get_close_matches(normalized, _CATEGORY_MAP.keys(), n=1, cutoff=0.75)
    return close[0] if close else normalized

# Words to strip when pulling the "thing" out of a free-form question
# like "tell me if any government school near me" -> "government school".
# Not a category whitelist — just filler that surrounds the real noun
# phrase in typical everyday phrasing.
_STOPWORDS = {
    "tell", "me", "if", "any", "is", "are", "there", "can", "you", "find",
    "do", "know", "i", "want", "to", "please", "show", "some", "the",
    "whether", "exists", "exist", "a", "an", "of", "could", "would",
    "give", "list", "get", "for", "us", "my", "search", "good", "best",
    "where", "what", "which", "located", "location", "nearest", "closest",
    "about", "most", "delicious", "tasty", "yummy", "popular", "famous", "top",
}

_NEARBY_TAIL_RE = re.compile(
    r"\b(?:near me|nearby|close by|around me|around here|near here|in my area|in my city)\b",
    re.IGNORECASE,
)


def extract_nearby_query(message: str) -> str | None:
    """Pulls the 'thing' the user is asking about out of a free-form
    "<thing> near me" style question — no fixed category list, so this
    works equally for "restaurants", "government schools", "mosques",
    "dentists", or anything else. Returns None if the message doesn't
    actually contain a nearby-style phrase at all."""
    m = _NEARBY_TAIL_RE.search(message)
    if not m:
        return None
    before = message[:m.start()]
    words = [w.strip(",.?!") for w in before.split()]
    while words and words[0].lower() in _STOPWORDS:
        words.pop(0)
    while words and words[-1].lower() in _STOPWORDS:
        words.pop()
    phrase = " ".join(words).strip()
    return phrase or None


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_nearby(lat: float, lon: float, category: str, radius_m: int = 1500, limit: int = 8) -> dict:
    key = _normalize_category(category)
    if key in _CATEGORY_MAP:
        return _overpass_nearby(lat, lon, key, radius_m=radius_m, limit=limit)
    # Not a well-known tag category — fall back to generic text search,
    # with a wider radius since Nominatim's relevance ranking isn't as
    # tight as a precise tag lookup.
    return _nominatim_nearby(lat, lon, category, radius_m=max(radius_m, 3000), limit=limit)


def _overpass_nearby(lat: float, lon: float, category: str, radius_m: int = 1500, limit: int = 8) -> dict:
    tags = _CATEGORY_MAP.get(category, DEFAULT_CATEGORY_KEYS)
    clauses = "".join(f'node[{t}](around:{radius_m},{lat},{lon});' for t in tags)
    query = f"[out:json][timeout:15];({clauses});out center {limit * 3};"

    # The free public Overpass instances are shared/rate-limited and
    # occasionally slow to the point of a gateway timeout — try each
    # mirror in turn instead of giving up after the first one struggles.
    last_error = None
    resp = None
    for url in OVERPASS_URLS:
        try:
            resp = httpx.post(
                url,
                data={"data": query},
                headers={"User-Agent": "Cipher-personal-assistant/1.0", "Accept": "application/json"},
                timeout=20,
            )
            resp.raise_for_status()
            last_error = None
            break
        except Exception as e:
            last_error = e
            resp = None
            continue

    if resp is None:
        return {"error": f"Couldn't reach the places lookup (all mirrors busy/unreachable): {last_error}"}

    elements = resp.json().get("elements", [])
    found = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        plat, plon = el.get("lat"), el.get("lon")
        if plat is None or plon is None:
            continue
        dist = _haversine_m(lat, lon, plat, plon)
        addr_bits = [
            tags.get("addr:housenumber"),
            tags.get("addr:street"),
            tags.get("addr:city") or tags.get("addr:suburb"),
        ]
        address = " ".join(b for b in addr_bits[:2] if b)
        if addr_bits[2]:
            address = f"{address}, {addr_bits[2]}" if address else addr_bits[2]
        found.append({
            "name": name,
            "distance_m": round(dist),
            "cuisine": tags.get("cuisine"),
            "address": address or None,
            "lat": plat,
            "lon": plon,
        })

    found.sort(key=lambda p: p["distance_m"])
    seen, unique = set(), []
    for p in found:
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        unique.append(p)
        if len(unique) >= limit:
            break

    if not unique:
        return {"error": f"Couldn't find any {category} within {radius_m}m of you."}

    return {"ok": True, "places": unique, "radius_m": radius_m}


def _nominatim_nearby(lat: float, lon: float, query: str, radius_m: int = 3000, limit: int = 8) -> dict:
    """Generic fallback for anything not in _CATEGORY_MAP — free-text
    search via Nominatim, bounded to a box around the user's coordinates
    so results stay local, then sorted by real distance."""
    delta_lat = radius_m / 111_000
    delta_lon = radius_m / (111_000 * max(0.2, math.cos(math.radians(lat))))
    viewbox = f"{lon - delta_lon},{lat + delta_lat},{lon + delta_lon},{lat - delta_lat}"

    try:
        resp = httpx.get(
            NOMINATIM_SEARCH_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "viewbox": viewbox,
                "bounded": 1,
                "limit": limit * 3,
            },
            headers={"Accept-Language": "en", "User-Agent": "Cipher-personal-assistant/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        return {"error": f"Couldn't reach the places lookup: {e}"}

    found = []
    for r in resp.json():
        try:
            plat, plon = float(r["lat"]), float(r["lon"])
        except (KeyError, ValueError, TypeError):
            continue
        display_name = r.get("display_name", "")
        parts = [p.strip() for p in display_name.split(",") if p.strip()]
        name = r.get("name") or (parts[0] if parts else None)
        if not name:
            continue
        dist = _haversine_m(lat, lon, plat, plon)
        # display_name's first part is usually the name itself; the rest
        # (road, area, city) makes a real human-readable address.
        address = ", ".join(parts[1:4]) if len(parts) > 1 else None
        found.append({"name": name, "distance_m": round(dist), "cuisine": None, "address": address, "lat": plat, "lon": plon})

    found.sort(key=lambda p: p["distance_m"])
    seen, unique = set(), []
    for p in found:
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        unique.append(p)
        if len(unique) >= limit:
            break

    if not unique:
        return {"error": f"Couldn't find any {query} within {radius_m}m of you."}

    return {"ok": True, "places": unique, "radius_m": radius_m}

#!/usr/bin/env python3
"""Helper functions for the inherited-globe notebook.

The notebook stays readable and focuses on orchestration. Reusable helpers for
parsing the UNESCO World Heritage List export, placing label points, resolving
Wikipedia articles, harvesting pageviews, and exporting GeoJSON live here.

Adapted from the sister project `endangered-globe`. The spatial machinery is gone:
UNESCO publishes one official coordinate pair per property (plus one per component
for serial properties), so there are no shapefiles to clean and no centroids to
compute from polygons.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
import urllib.parse
from pathlib import Path

import pandas as pd
import requests
from tqdm.notebook import tqdm

# ── Runtime configuration. The notebook calls configure() after defining its knobs. ──
USER_AGENT = "InheritedGlobe/1.0 (https://github.com/tdemareuil/inherited-globe)"
WIKIMEDIA_TOKEN = ""  # Optional — raises rate limit from 500 to 5,000 req/hour
SLEEP_WIKI = 0.15       # Wikimedia action/REST API (token-authenticated, ~6.6 req/s)
SLEEP_PAGEVIEWS = 1.0   # AQS pageviews endpoint (IP-based limit, token not honoured)
SLEEP_SPARQL = 1.0      # Wikidata Query Service: be gentle
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
PAGEVIEWS_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
PAGEVIEWS_AGENT = "user"  # "user" = human only, "all-agents" = humans + bots
WIKIPEDIA_SUMMARY_URL = "https://{project}/api/rest_v1/page/summary/{title}"
UNESCO_SITE_URL = "https://whc.unesco.org/en/list/{site_id}"
START = ""
END = ""

# Language priority for the Wikipedia article behind each site's pageview count.
# UNESCO properties are global, so the list is wider than a purely European one.
WIKIPEDIA_LANGUAGE_PRIORITY = [
    "en", "de", "fr", "es", "it", "ru", "ja", "zh", "pt", "nl",
    "pl", "ar", "uk", "sv", "tr", "ko", "fa", "cs", "id", "vi",
    "hi", "he", "fi", "da", "no", "hu", "ro", "el", "ca", "th",
]
WIKIPEDIA_LANGUAGE_RANK = {lang: rank for rank, lang in enumerate(WIKIPEDIA_LANGUAGE_PRIORITY)}

WIKIDATA_FIELDS = [
    "wiki_title",
    "wiki_language",
    "wiki_project",
    "wiki_url",
    "wikidata_url",
    "wikidata_image_url",
]

# UNESCO's five programme regions, as written in the official export.
UNESCO_REGIONS = [
    "Africa",
    "Arab States",
    "Asia and the Pacific",
    "Europe and North America",
    "Latin America and the Caribbean",
]

# Category → color key used by index.html. Sites on the List of World Heritage in
# Danger take the "Danger" key instead, so the red dot overrides their category color.
CATEGORIES = ["Cultural", "Natural", "Mixed"]
DANGER_COLOR_KEY = "Danger"


def configure(**kwargs):
    """Set runtime values supplied by the notebook configuration cell."""
    globals().update({key: value for key, value in kwargs.items() if value is not None})


def set_pageview_window(start, end):
    """Set the Wikimedia pageview window used by get_pageviews()."""
    configure(START=start, END=end)


def read_local_secret(path):
    """Read a local secret file ignored by git, returning an empty string if absent."""
    path = Path(path)
    return path.read_text().strip() if path.exists() else ""


def clean_str(value):
    """Return a stripped string, or '' for None/NaN/non-string values."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in {"nan", "none", "<na>"} else s


def wikimedia_headers():
    """Build request headers for Wikimedia APIs, injecting Bearer token when configured."""
    headers = {"User-Agent": USER_AGENT}
    token = (WIKIMEDIA_TOKEN or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def wikimedia_retry_after(response, default=10):
    """Return seconds to wait from a Wikimedia 429 Retry-After header, or default."""
    header = response.headers.get("Retry-After", "")
    if header:
        try:
            return int(header)
        except ValueError:
            pass
    return default


def pick_path(obj, *paths):
    """Return the first non-empty value found at any of the given nested key paths."""
    for path in paths:
        current = obj
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current:
            return current
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 1. UNESCO export parsing
# ══════════════════════════════════════════════════════════════════════════════

def strip_tags(value):
    """Drop the occasional inline HTML (<em>, <p>, <br>) the UNESCO CMS leaves in text."""
    text = clean_str(value)
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]*>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def parse_coordinates(value):
    """Parse the export's 'Coordonnées' field ('lat, lon') into a (lat, lon) tuple.

    Returns (None, None) when the field is empty or unparseable. Sites inscribed at the
    most recent session sometimes ship without it, and fall back to their components.
    """
    text = clean_str(value)
    if not text:
        return (None, None)
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if len(parts) < 2:
        return (None, None)
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return (None, None)
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return (None, None)
    return (lat, lon)


# The export writes components as a pseudo-JSON list with unquoted keys:
#   {name: Hanklit, ref: 1758-001, latitude: 56.895649, longitude: 8.751962}, {...}
_COMPONENT_RE = re.compile(
    r"\{\s*name:\s*(?P<name>.*?),\s*ref:\s*(?P<ref>.*?),\s*"
    r"latitude:\s*(?P<lat>-?[\d.]+)\s*,\s*longitude:\s*(?P<lon>-?[\d.]+)\s*\}",
    re.S,
)


def parse_components(value):
    """Parse the export's 'Components' field into a list of {name, ref, lat, lon} dicts."""
    text = clean_str(value)
    if not text:
        return []
    components = []
    for match in _COMPONENT_RE.finditer(text):
        try:
            lat, lon = float(match.group("lat")), float(match.group("lon"))
        except ValueError:
            continue
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            continue
        components.append(
            {
                "name": match.group("name").strip(),
                "ref": match.group("ref").strip(),
                "lat": lat,
                "lon": lon,
            }
        )
    return components


def parse_url_list(value):
    """Split the export's comma-separated image/video URL fields into a clean list."""
    text = clean_str(value)
    if not text:
        return []
    seen, urls = set(), []
    for candidate in re.split(r"[,\s]+", text):
        candidate = candidate.strip().rstrip(",")
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        candidate = re.sub(r"^http://", "https://", candidate, flags=re.I)
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
    return urls


def unesco_site_url(site_id):
    """Build the canonical whc.unesco.org page URL for a site ID."""
    sid = clean_str(site_id)
    if not sid.isdigit():
        return None
    return UNESCO_SITE_URL.format(site_id=sid)


def category_color_key(category, in_danger):
    """Return the color key used by index.html: danger overrides the category color."""
    if in_danger:
        return DANGER_COLOR_KEY
    category = clean_str(category).title()
    return category if category in CATEGORIES else "Cultural"


# Trailing clauses UNESCO uses to qualify a property, dropped from the globe label
# because the full inscription name is often a sentence. The full name stays in the popup.
_LABEL_SPLIT_TOKENS = [" – ", " — ", " -- ", ": ", " and Associated ", " and its Associated "]


def short_label(name, max_chars=42):
    """Shorten an inscription name for on-globe display, keeping the popup name intact.

    UNESCO names run long ('Memphis and its Necropolis – the Pyramid Fields from Giza to
    Dahshur'). The globe reads better with the head of the name; the popup keeps the
    official one. Returns None when the name is already short enough.
    """
    full = strip_tags(name)
    if not full or len(full) <= max_chars:
        return None

    head = full
    for token in _LABEL_SPLIT_TOKENS:
        if token in head:
            candidate = head.split(token, 1)[0].strip()
            if len(candidate) >= 8:
                head = candidate
                break
    if len(head) <= max_chars:
        return head if head != full else None

    # Still long: cut at the last comma, then at the last word boundary.
    if "," in head[:max_chars + 12]:
        candidate = head[: max_chars + 12].rsplit(",", 1)[0].strip()
        if len(candidate) >= 8:
            return candidate
    cut = head[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(" ,;:-–—") + "…"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Label points
# ══════════════════════════════════════════════════════════════════════════════

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def mean_position(points):
    """Average a list of (lat, lon) points on the sphere, so antimeridian groups behave."""
    if not points:
        return (None, None)
    xs = ys = zs = 0.0
    for lat, lon in points:
        phi, lam = math.radians(lat), math.radians(lon)
        xs += math.cos(phi) * math.cos(lam)
        ys += math.cos(phi) * math.sin(lam)
        zs += math.sin(phi)
    n = len(points)
    xs, ys, zs = xs / n, ys / n, zs / n
    hyp = math.hypot(xs, ys)
    if hyp < 1e-12 and abs(zs) < 1e-12:
        return points[0]
    return (math.degrees(math.atan2(zs, hyp)), math.degrees(math.atan2(ys, xs)))


def cluster_points(points, buffer_km):
    """Single-linkage clustering of (lat, lon) points by great-circle distance.

    Mirrors the range-cluster step of the sister project: components of one serial
    property that sit close together become a single label point.
    """
    if not points:
        return []
    remaining = list(range(len(points)))
    clusters = []
    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        changed = True
        while changed:
            changed = False
            for idx in list(remaining):
                lat, lon = points[idx]
                if any(haversine_km(lat, lon, *points[member]) <= buffer_km for member in cluster):
                    cluster.append(idx)
                    remaining.remove(idx)
                    changed = True
        clusters.append(cluster)
    clusters.sort(key=len, reverse=True)
    return clusters


def site_label_points(main_lat, main_lon, components, buffer_km, max_points,
                      secondary_min_share):
    """Return the label points for one property, most representative first.

    - No components: the official site coordinates are the only point.
    - Components: they are clustered by distance. The largest cluster always gets a
      point (snapped to the official coordinates when those fall inside it), then
      secondary clusters get one only if they hold at least `secondary_min_share` of
      the components, capped at `max_points`.
    """
    has_main = main_lat is not None and main_lon is not None
    if not components:
        if not has_main:
            return []
        return [
            {
                "lat": main_lat,
                "lon": main_lon,
                "point_source": "site_coordinates",
                "cluster_component_count": 0,
                "cluster_share": 1.0,
            }
        ]

    coords = [(c["lat"], c["lon"]) for c in components]
    clusters = cluster_points(coords, buffer_km)
    total = len(coords)
    label_points = []
    for rank, cluster in enumerate(clusters, start=1):
        share = len(cluster) / total
        if rank > 1 and (share < secondary_min_share or len(label_points) >= max_points):
            continue
        members = [coords[i] for i in cluster]
        if rank == 1 and has_main and any(
            haversine_km(main_lat, main_lon, lat, lon) <= buffer_km for lat, lon in members
        ):
            lat, lon = main_lat, main_lon
            source = "site_coordinates"
        else:
            lat, lon = mean_position(members)
            source = "component_cluster"
        label_points.append(
            {
                "lat": lat,
                "lon": lon,
                "point_source": source,
                "cluster_component_count": len(cluster),
                "cluster_share": round(share, 4),
            }
        )
        if len(label_points) >= max_points:
            break

    if not label_points and has_main:
        return [
            {
                "lat": main_lat,
                "lon": main_lon,
                "point_source": "site_coordinates",
                "cluster_component_count": 0,
                "cluster_share": 1.0,
            }
        ]
    return label_points


def jitter_duplicate_points(df, lat_col="lat", lon_col="lon", key_col="site_id",
                            offset_km=3.0):
    """Nudge apart label points that land on the exact same coordinates.

    Two properties occasionally share a published coordinate pair (adjacent inscriptions,
    or a rounded value). MapLibre's de-overlap would then permanently hide one of the two.
    """
    df = df.copy()
    grouped = df.groupby([df[lat_col].round(6), df[lon_col].round(6)])
    moved = 0
    for _, idx in grouped.groups.items():
        rows = list(idx)
        if len(rows) < 2:
            continue
        for offset, row in enumerate(rows[1:], start=1):
            lat = float(df.at[row, lat_col])
            angle = 2 * math.pi * offset / max(1, len(rows) - 1)
            dlat = (offset_km / 111.32) * math.cos(angle)
            dlon = (offset_km / (111.32 * max(0.15, math.cos(math.radians(lat))))) * math.sin(angle)
            df.at[row, lat_col] = max(-89.5, min(89.5, lat + dlat))
            df.at[row, lon_col] = ((float(df.at[row, lon_col]) + dlon + 180) % 360) - 180
            moved += 1
    if moved:
        print(f"  [jitter] moved {moved} overlapping label points by ~{offset_km} km")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 3. Wikidata resolution
# ══════════════════════════════════════════════════════════════════════════════

def article_rank(article_lang):
    """Rank Wikipedia languages; any unlisted language remains usable after the preferred list."""
    return WIKIPEDIA_LANGUAGE_RANK.get(str(article_lang), len(WIKIPEDIA_LANGUAGE_PRIORITY))


# abstract.wikipedia.org is a Wikimedia project, not a Wikipedia edition, and has no
# pageview history — excluded explicitly rather than by the generic domain filter.
_SITELINK_BLOCK = 'FILTER(!CONTAINS(STR(?wiki_site), "abstract.wikipedia.org"))'


def build_sparql_query(whc_ids):
    """Batch SPARQL: resolve UNESCO World Heritage site IDs to sitelinks and images."""
    values = " ".join(f'"{i}"' for i in whc_ids)
    return f"""
SELECT ?whc_id ?item ?article ?article_lang ?wiki_project ?article_title ?wikidata_image_url WHERE {{
  VALUES ?whc_id {{ {values} }}
  ?item wdt:P757 ?whc_id .             # P757 = World Heritage Site ID
  OPTIONAL {{ ?item wdt:P18 ?wikidata_image_url . }}   # P18 = image
  ?article schema:about ?item ;
            schema:inLanguage ?article_lang ;
            schema:isPartOf ?wiki_site .
  FILTER(CONTAINS(STR(?wiki_site), ".wikipedia.org/"))
  {_SITELINK_BLOCK}
  BIND(REPLACE(STR(?wiki_site), "^https?://", "") AS ?wiki_project_slash)
  BIND(REPLACE(?wiki_project_slash, "/$", "") AS ?wiki_project)
  BIND(REPLACE(STR(?article), CONCAT("https://", ?wiki_project, "/wiki/"), "") AS ?article_title)
}}
"""


def build_sparql_qid_query(qids):
    """SPARQL to get Wikipedia sitelinks for a known list of Wikidata QIDs."""
    values = " ".join(f"wd:{qid}" for qid in qids)
    return f"""
SELECT ?item ?article ?article_lang ?wiki_project ?article_title ?wikidata_image_url WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item wdt:P18 ?wikidata_image_url . }}
  ?article schema:about ?item ;
            schema:inLanguage ?article_lang ;
            schema:isPartOf ?wiki_site .
  FILTER(CONTAINS(STR(?wiki_site), ".wikipedia.org/"))
  {_SITELINK_BLOCK}
  BIND(REPLACE(STR(?wiki_site), "^https?://", "") AS ?wiki_project_slash)
  BIND(REPLACE(?wiki_project_slash, "/$", "") AS ?wiki_project)
  BIND(REPLACE(STR(?article), CONCAT("https://", ?wiki_project, "/wiki/"), "") AS ?article_title)
}}
"""


def _entry_from_sparql_row(row, item_key="item"):
    """Build a wikidata_map entry dict from one SPARQL result row."""
    image = (row.get("wikidata_image_url") or {}).get("value")
    return {
        "wikidata_url": row[item_key]["value"].replace("http://", "https://", 1),
        "wiki_title": urllib.parse.unquote(row["article_title"]["value"]),
        "wiki_language": row["article_lang"]["value"],
        "wiki_project": row["wiki_project"]["value"],
        "wiki_url": row["article"]["value"],
        "wiki_rank": article_rank(row["article_lang"]["value"]),
        "wikidata_image_url": image.replace("http://", "https://", 1) if image else None,
        "wiki_lookup_source": "wikidata_P757",
    }


def _merge_entry(mapping, key, candidate):
    """Merge a candidate entry into mapping, keeping the best-ranked language."""
    existing = mapping.get(key)
    if existing is None:
        mapping[key] = candidate
        return
    if candidate["wiki_rank"] < existing["wiki_rank"]:
        existing.update({
            k: candidate[k]
            for k in ("wiki_title", "wiki_language", "wiki_project", "wiki_url", "wiki_rank")
        })
    if candidate.get("wikidata_image_url") and not existing.get("wikidata_image_url"):
        existing["wikidata_image_url"] = candidate["wikidata_image_url"]


def _post_sparql(sparql, retries=4, timeout=90):
    """POST a SPARQL query, backing off on 429/5xx."""
    for attempt in range(retries):
        try:
            response = requests.post(
                WIKIDATA_ENDPOINT,
                data={"query": sparql},
                headers={
                    **wikimedia_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/sparql-results+json",
                },
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            if attempt == retries - 1:
                raise
            tqdm.write(f"  [sparql] {exc} — retrying")
            time.sleep(2 ** attempt * 3)
            continue
        if response.status_code in (429, 500, 502, 503, 504):
            wait = wikimedia_retry_after(response, default=2 ** attempt * 5)
            tqdm.write(f"  [sparql] HTTP {response.status_code} — waiting {wait}s")
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()
    return None


def query_wikidata_batch(whc_ids, batch_size=300):
    """Run the P757 SPARQL lookup in batches to stay under the query size limit."""
    mapping = {}
    ids = [str(i) for i in whc_ids]
    for start in tqdm(range(0, len(ids), batch_size), desc="Wikidata batches"):
        batch = ids[start:start + batch_size]
        data = _post_sparql(build_sparql_query(batch))
        if not data:
            continue
        for row in data["results"]["bindings"]:
            _merge_entry(mapping, row["whc_id"]["value"], _entry_from_sparql_row(row))
        time.sleep(SLEEP_SPARQL)
    return mapping


def wikidata_entity_search(search_term, retries=3):
    """Search Wikidata by label and return the best entry with a Wikipedia sitelink."""
    for attempt in range(retries):
        try:
            response = requests.get(
                WIKIDATA_API_URL,
                params={
                    "action": "wbsearchentities",
                    "search": search_term[:250],
                    "language": "en",
                    "type": "item",
                    "format": "json",
                    "limit": 5,
                },
                headers=wikimedia_headers(),
                timeout=20,
            )
            if response.status_code == 429:
                wait = wikimedia_retry_after(response, default=2 ** attempt * 3)
                tqdm.write(f"  [wbsearchentities] 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            break
        except requests.exceptions.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt * 2)
    else:
        return None

    qids = [hit["id"] for hit in response.json().get("search", [])]
    if not qids:
        return None
    data = _post_sparql(build_sparql_qid_query(qids), timeout=45)
    rows = (data or {}).get("results", {}).get("bindings", [])
    if not rows:
        return None
    best = min(rows, key=lambda row: article_rank(row["article_lang"]["value"]))
    entry = _entry_from_sparql_row(best)
    entry["wiki_lookup_source"] = "wikidata_entity_search"
    return entry


def wikipedia_direct_search(search_term, lang="en", retries=3):
    """Check whether a Wikipedia page exists for search_term, resolving redirects."""
    for attempt in range(retries):
        try:
            response = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "query", "titles": search_term, "redirects": True, "format": "json"},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            if response.status_code == 429:
                wait = wikimedia_retry_after(response, default=2 ** attempt * 3)
                tqdm.write(f"  [wikipedia direct] 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            break
        except requests.exceptions.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt * 2)
    else:
        return None

    pages = response.json().get("query", {}).get("pages", {})
    for page_id, page in pages.items():
        if page_id == "-1":
            return None
        title = page.get("title", search_term)
        project = f"{lang}.wikipedia.org"
        return {
            "wiki_title": title,
            "wiki_language": lang,
            "wiki_project": project,
            "wiki_url": f"https://{project}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
            "wikidata_url": None,
            "wikidata_image_url": None,
            "wiki_rank": article_rank(lang),
            "wiki_lookup_source": f"wikipedia_direct_{lang}",
        }
    return None


def name_variants(name):
    """Return lookup candidates for a site name, from most to least specific.

    The export's English names carry qualifiers Wikipedia usually drops: a trailing
    country in parentheses, a leading 'The ', an em-dash subtitle.
    """
    base = strip_tags(name)
    if not base:
        return []
    variants = [base]
    without_parens = re.sub(r"\s*\([^)]*\)\s*$", "", base).strip()
    variants.append(without_parens)
    for token in _LABEL_SPLIT_TOKENS:
        if token in without_parens:
            variants.append(without_parens.split(token, 1)[0].strip())
    if without_parens.lower().startswith("the "):
        variants.append(without_parens[4:].strip())
    # Some names are written with a leading article-style prefix UNESCO uses in titles.
    for prefix in ("Historic Centre of ", "Historic City of ", "Old Town of ", "City of ",
                   "Historical Centre of ", "Old City of "):
        if without_parens.startswith(prefix):
            variants.append(without_parens[len(prefix):].strip())

    seen, out = set(), []
    for variant in variants:
        variant = variant.strip(" ,;:-–—")
        key = variant.lower()
        if len(variant) >= 4 and key not in seen:
            seen.add(key)
            out.append(variant)
    return out


def resolve_sites_by_name(unresolved, cache_path=None, languages=("en",)):
    """Fallback chain for sites whose P757 ID is not in Wikidata.

    `unresolved` is an iterable of (site_id, name_en, states) tuples.

    Pass 1 — Wikidata entity search on each name variant.
    Pass 2 — direct Wikipedia title lookup on each name variant, per language.

    cache_path: optional JSON file; already-resolved sites are skipped and results are
    written incrementally so an interrupted run can be resumed.
    """
    cache_path = Path(cache_path) if cache_path else None
    mapping = {}
    if cache_path and cache_path.exists():
        mapping = json.loads(cache_path.read_text())
        print(f"  [name fallback] resuming with {len(mapping)} cached entries")

    pending = [(str(sid), name, states) for sid, name, states in unresolved
               if str(sid) not in mapping]
    if not pending:
        return mapping

    def save():
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=1))

    for site_id, name, states in tqdm(pending, desc="Name fallback"):
        entry = None
        variants = name_variants(name)
        for variant in variants:
            entry = wikidata_entity_search(variant)
            time.sleep(SLEEP_WIKI)
            if entry:
                break
        if entry is None:
            for lang in languages:
                for variant in variants:
                    entry = wikipedia_direct_search(variant, lang=lang)
                    time.sleep(SLEEP_WIKI)
                    if entry:
                        break
                if entry:
                    break
        if entry:
            mapping[site_id] = entry
            if len(mapping) % 25 == 0:
                save()
    save()
    print(f"  [name fallback] resolved {len(mapping)} sites")
    return mapping


def attach_wikidata_fields(frame, mapping, id_col="site_id"):
    """Attach wiki_* / wikidata_* columns onto a DataFrame from a site-ID mapping."""
    frame = frame.copy()
    keys = frame[id_col].astype(str)
    for field in WIKIDATA_FIELDS:
        frame[field] = keys.map(lambda k: (mapping.get(k) or {}).get(field))
    frame["wiki_lookup_source"] = keys.map(
        lambda k: (mapping.get(k) or {}).get("wiki_lookup_source")
    )
    return frame


# ══════════════════════════════════════════════════════════════════════════════
# 4. Wikimedia pageviews & images
# ══════════════════════════════════════════════════════════════════════════════

def get_pageviews(project, title, retries=4):
    """Return total Wikipedia views over the configured window for one project/title."""
    if not clean_str(title):
        return 0
    encoded = urllib.parse.quote(str(title), safe="")
    project = project or "en.wikipedia.org"
    url = f"{PAGEVIEWS_BASE}/{project}/all-access/{PAGEVIEWS_AGENT}/{encoded}/monthly/{START}/{END}"
    for _ in range(retries):
        try:
            response = requests.get(url, headers=wikimedia_headers(), timeout=20)
            if response.status_code == 404:
                return 0
            if response.status_code == 429:
                wait = wikimedia_retry_after(response, default=30)
                tqdm.write(f"  [pageviews] 429 for {title!r} — waiting {wait}s")
                time.sleep(wait)
                continue
            if not response.ok:
                tqdm.write(f"  [pageviews] HTTP {response.status_code} for {title!r}")
                return 0
            return sum(item["views"] for item in response.json().get("items", []))
        except Exception as exc:
            tqdm.write(f"  [pageviews] error for {title!r}: {exc}")
            return 0
    tqdm.write(f"  [pageviews] gave up after {retries} retries for {title!r}")
    return 0


def get_wikipedia_thumbnail(project, title, retries=4):
    """Return a Wikipedia thumbnail/original image URL for a page title, or None."""
    if not clean_str(title):
        return None
    project = project or "en.wikipedia.org"
    url = WIKIPEDIA_SUMMARY_URL.format(project=project, title=urllib.parse.quote(str(title), safe=""))
    for attempt in range(retries):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                wait = wikimedia_retry_after(response, default=2 ** attempt * 5)
                tqdm.write(f"  [thumbnail] 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            image = pick_path(response.json(), ("originalimage", "source"), ("thumbnail", "source"))
            return image.replace("http://", "https://", 1) if image else None
        except requests.exceptions.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt * 2)
    return None


def wikimedia_tiff_to_thumbnail(url, width=1000):
    """Rewrite a Commons TIFF/SVG file URL to a JPEG/PNG thumbnail browsers can display."""
    text = clean_str(url)
    if not text:
        return None
    lowered = text.lower()
    if not lowered.endswith((".tif", ".tiff", ".svg")):
        return text
    match = re.search(r"/commons/(?:thumb/)?([0-9a-f])/([0-9a-f]{2})/([^/]+)$", text)
    if not match:
        return text
    a, b, filename = match.groups()
    suffix = "png" if lowered.endswith(".svg") else "jpg"
    return (f"https://upload.wikimedia.org/wikipedia/commons/thumb/{a}/{b}/{filename}"
            f"/{width}px-{filename}.{suffix}")


# ══════════════════════════════════════════════════════════════════════════════
# 5. GeoJSON export
# ══════════════════════════════════════════════════════════════════════════════

# Properties written to sites.geojson. Anything else stays in the notebook's DataFrame.
GEOJSON_FIELDS = [
    "site_id", "label", "short_label", "states", "iso_codes", "region",
    "category", "color_key", "in_danger", "date_inscribed", "criteria",
    "area_hectares", "short_description", "component_count",
    "unesco_url", "image_url", "extra_image_urls", "image_credit", "image_source",
    "wiki_title", "wiki_language", "wiki_project", "wiki_url",
    "wikidata_url", "wikidata_image_url", "wikipedia_thumbnail_url",
    "popularity", "label_rank", "label_count", "point_source",
    "cluster_component_count", "cluster_share",
]

INT_FIELDS = {"site_id", "date_inscribed", "component_count", "popularity",
              "label_rank", "label_count", "cluster_component_count"}
FLOAT_FIELDS = {"area_hectares", "cluster_share"}
BOOL_FIELDS = {"in_danger"}


def clean_json_value(value):
    """Normalise a value for JSON: NaN/NaT/empty become None, numpy scalars become Python."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def feature_properties(row):
    """Build the properties dict for one GeoJSON feature, with stable types."""
    props = {}
    for field in GEOJSON_FIELDS:
        value = clean_json_value(row.get(field))
        if value is None:
            continue
        if field in INT_FIELDS:
            try:
                value = int(round(float(value)))
            except (TypeError, ValueError):
                continue
        elif field in FLOAT_FIELDS:
            try:
                value = round(float(value), 4)
            except (TypeError, ValueError):
                continue
        elif field in BOOL_FIELDS:
            value = bool(value)
        elif field == "extra_image_urls" and isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        props[field] = value
    return props


def build_geojson(df, lat_col="lat", lon_col="lon"):
    """Turn the finished DataFrame into a GeoJSON FeatureCollection of Point features."""
    features = []
    for _, row in df.iterrows():
        lat, lon = row.get(lat_col), row.get(lon_col)
        if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]},
            "properties": feature_properties(row),
        })
    return {"type": "FeatureCollection", "features": features}


def write_geojson(geojson, path):
    """Write a FeatureCollection to disk and report its size."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"Wrote {len(geojson['features']):,} features to {path} ({size_mb:.2f} MB)")
    return path


def fold_for_search(value):
    """Lowercase and strip accents — mirrors foldForSearch() in index.html."""
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn").lower()
    for src, dst in (("ø", "o"), ("ß", "ss"), ("ı", "i"), ("æ", "ae"), ("þ", "th")):
        text = text.replace(src, dst)
    return text

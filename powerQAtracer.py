#!/usr/bin/env python3
"""Download global Osmose power-line issues at or above a voltage threshold.

Supported issues:
  unfinished          Osmose 7040 class 2 = "Unfinished power transmission line"
  unmatched-voltage   Osmose 7040 class 7 = "Unmatched voltage of line on substation"
  different-voltages  own Overpass check: power lines with different voltages sharing a node.
                      Osmose's class 3 issues carry no voltages and Osmose skips many cases,
                      so this one reports every such node, labelled but unfiltered.

The output is a GeoJSON FeatureCollection that can be opened in JOSM.

The Osmose issues API returns at most 500 issues per request. This script therefore
queries the planet in tiles and recursively subdivides any tile that reaches the
limit. Every issue is stored unfiltered in a SQLite checkpoint database, so an
interrupted run can be resumed and the voltage threshold can be changed later
without downloading anything again. Pass --reset-state to fetch fresh data.

Osmose only embeds the power and voltage tags of unfinished lines, so the kept
elements are looked up on Overpass to drop deleted, disused or substation-internal
ones. If Overpass is unavailable the affected issues are kept with osm_verified=false.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

import requests

API_URL = "https://osmose.openstreetmap.fr/api/0.3/issues.geojson"
OSMOSE_MAP_URL = "https://osmose.openstreetmap.fr/en/map/"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
JOSM_REMOTE_URL = "http://127.0.0.1:8111/load_and_zoom"
JOSM_ZOOM_DEG = 0.005
OVERPASS_BATCH_SIZE = 1000
ITEM = 7040
API_LIMIT = 500
STATE_VERSION = 3

LINE_POWER_VALUES = {"line", "minor_line", "cable"}
LIFECYCLE_PREFIXES = (
    "abandoned",
    "construction",
    "demolished",
    "disused",
    "planned",
    "proposed",
    "razed",
    "removed",
)
INTERNAL_LINE_VALUES = {"bay", "busbar", "substation"}


@dataclass(frozen=True)
class IssueType:
    osmose_class: int
    title: str
    file_stem: str
    # A dangling busbar is normal, but a busbar above its substation's voltage is still a conflict.
    exclude_internal_lines: bool
    source: str = "osmose"

    @property
    def file_prefix(self) -> str:
        return "osmose" if self.source == "osmose" else "powerQAtracer"


ISSUE_TYPES = {
    "unfinished": IssueType(2, "Unfinished power major line", "unfinished_lines", True),
    "unmatched-voltage": IssueType(
        7, "Unmatched voltage of line on substation", "unmatched_voltages", False
    ),
    "different-voltages": IssueType(
        3, "Connection between different voltages", "different_voltages", False, "overpass"
    ),
}

# Ranking of junction kinds: a voltage that just stops is the likeliest error.
JUNCTION_KINDS = {
    "voltage_ends": "A voltage is present on only one of the connected lines",
    "missing_voltage": "At least one connected line has no voltage tag",
    "voltage_split": "Voltage sets differ, but every voltage continues on another line",
}
JUNCTION_KIND_ORDER = {kind: i for i, kind in enumerate(JUNCTION_KINDS)}
JUNCTION_NODE_TAGS = ("power", "line_management", "transformer", "location:transition")
OVERPASS_ID_BATCH = 2000


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Fetch global Osmose item 7040 power-line issues and keep only issues "
            "whose line voltage is at or above the threshold."
        )
    )
    p.add_argument(
        "--issue",
        choices=sorted(ISSUE_TYPES),
        default="unfinished",
        help="Issue to export (default: %(default)s)",
    )
    p.add_argument(
        "-o",
        "--output",
        default=None,
        help=(
            "Output GeoJSON path (default: <osmose|powerQAtracer>_<issue>_<threshold>kv_and_above"
            "[_<region>].geojson)"
        ),
    )
    p.add_argument(
        "--state",
        default=None,
        help=(
            "SQLite checkpoint path. Default: <osmose|powerQAtracer>_<issue>[_<kV>].state.sqlite "
            "next to the output. Existing state is reused automatically."
        ),
    )
    p.add_argument(
        "--reset-state",
        action="store_true",
        help="Delete any existing checkpoint and download fresh data.",
    )
    p.add_argument(
        "--min-voltage-kv",
        type=float,
        default=300.0,
        help="Keep line voltages at or above this value in kV (default: %(default)s)",
    )
    p.add_argument(
        "--include-missing-voltage",
        action="store_true",
        help="Also keep issues whose line has no parseable voltage tag.",
    )
    p.add_argument(
        "--keep-excluded",
        action="store_true",
        help="Keep deleted, disused and similar issues, labelled with exclusion_reason.",
    )
    p.add_argument(
        "--no-osm-check",
        action="store_true",
        help="Skip the Overpass lookup and rely only on the tags embedded by Osmose.",
    )
    p.add_argument(
        "--overpass-url",
        default=OVERPASS_URL,
        help="Overpass API endpoint (default: %(default)s)",
    )
    p.add_argument(
        "--tile-deg",
        type=float,
        default=10.0,
        help="Initial world tile size in degrees (default: %(default)s)",
    )
    p.add_argument(
        "--min-tile-deg",
        type=float,
        default=0.02,
        help="Smallest allowed tile size before aborting on a still-full API response",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="HTTP timeout per request in seconds (default: %(default)s)",
    )
    p.add_argument(
        "--request-delay",
        "--sleep",
        dest="request_delay",
        type=float,
        default=2.0,
        help=(
            "Minimum seconds between Osmose request starts (default: %(default)s). "
            "--sleep is kept as a backwards-compatible alias."
        ),
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=8,
        help="Maximum attempts for a failed/limited request (default: %(default)s)",
    )
    p.add_argument(
        "--retry-base-delay",
        type=float,
        default=5.0,
        help=(
            "Initial retry delay in seconds for HTTP 429/5xx/network errors; retries "
            "use exponential backoff and honor Retry-After (default: %(default)s)"
        ),
    )
    p.add_argument(
        "--bbox",
        default=None,
        help=(
            "Only download and export issues inside WEST,SOUTH,EAST,NORTH (degrees), "
            "e.g. 5.8,47.2,15.1,55.1"
        ),
    )
    p.add_argument(
        "--country",
        default=None,
        help=(
            "Only export issues in these Osmose countries, comma-separated. A prefix "
            "matches all subregions: germany matches germany_bayern, germany_hessen, ..."
        ),
    )
    p.add_argument(
        "--maproulette",
        default=None,
        metavar="PATH",
        help="Also write a line-delimited GeoJSON file with one MapRoulette task per issue.",
    )
    p.add_argument(
        "--compare-to",
        default=None,
        metavar="OLD_GEOJSON",
        help=(
            "Compare with an earlier output: label issues as new or open, report resolved "
            "ones and write them to <output>_resolved.geojson."
        ),
    )
    p.add_argument(
        "--levels",
        default="1,2,3",
        help="Osmose severity levels (default: %(default)s)",
    )
    return p.parse_args()


BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Region:
    bbox: BBox | None = None
    countries: tuple[str, ...] = ()

    def contains(self, point: tuple[float, float] | None, country: Any) -> bool:
        if self.bbox is not None:
            if point is None:
                return False
            west, south, east, north = self.bbox
            lon, lat = point
            if not (west <= lon <= east and south <= lat <= north):
                return False
        if self.countries:
            name = str(country or "").lower()
            if not any(name == c or name.startswith(c + "_") for c in self.countries):
                return False
        return True

    def intersects_tile(self, tile: BBox) -> bool:
        if self.bbox is None:
            return True
        west, south, east, north = self.bbox
        return tile[0] < east and tile[2] > west and tile[1] < north and tile[3] > south


def parse_region(args: argparse.Namespace) -> Region:
    bbox = None
    if args.bbox:
        try:
            values = tuple(float(v) for v in args.bbox.split(","))
        except ValueError:
            values = ()
        if len(values) != 4:
            raise SystemExit("--bbox must be WEST,SOUTH,EAST,NORTH")
        west, south, east, north = values
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise SystemExit("--bbox must satisfy -180 <= WEST < EAST <= 180 and -90 <= SOUTH < NORTH <= 90")
        bbox = (west, south, east, north)
    countries = tuple(
        c.strip().lower() for c in (args.country or "").split(",") if c.strip()
    )
    return Region(bbox, countries)


def point_of(feature: dict[str, Any]) -> tuple[float, float] | None:
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "Point" and len(coords) >= 2:
        return float(coords[0]), float(coords[1])
    return None


class RateLimiter:
    """Enforce a minimum interval between request *starts*."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = max(0.0, delay_s)
        self.last_request_started: float | None = None

    def wait(self) -> None:
        if self.last_request_started is not None and self.delay_s > 0:
            elapsed = time.monotonic() - self.last_request_started
            remaining = self.delay_s - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_started = time.monotonic()


def retry_after_seconds(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def request_json(
    session: requests.Session,
    limiter: RateLimiter,
    method: str,
    url: str,
    *,
    description: str,
    timeout: float,
    max_attempts: int,
    retry_base_delay: float,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        response: requests.Response | None = None
        try:
            limiter.wait()
            response = session.request(method, url, params=params, data=data, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Unexpected response: expected a JSON object")
            return result

        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt >= max_attempts:
                break

            server_delay = retry_after_seconds(response) if response is not None else None
            backoff = max(0.0, retry_base_delay) * (2 ** (attempt - 1))
            wait_s = max(backoff, server_delay or 0.0)
            wait_s = min(wait_s, 300.0)
            status = f"HTTP {response.status_code}" if response is not None else type(exc).__name__
            print(
                f"  Request failed ({status}) for {description}; retry {attempt + 1}/{max_attempts} "
                f"after {wait_s:.1f}s...",
                file=sys.stderr,
            )
            time.sleep(wait_s)

    raise RuntimeError(f"Request failed for {description}: {last_error}")


def request_geojson(
    session: requests.Session,
    limiter: RateLimiter,
    bbox: tuple[float, float, float, float],
    *,
    osmose_class: int,
    levels: str,
    timeout: float,
    max_attempts: int,
    retry_base_delay: float,
) -> dict[str, Any]:
    west, south, east, north = bbox
    data = request_json(
        session,
        limiter,
        "GET",
        API_URL,
        description=f"bbox={bbox}",
        params={
            "item": ITEM,
            "class": osmose_class,
            "level": levels,
            "status": "open",
            "limit": API_LIMIT,
            "bbox": f"{west:.8f},{south:.8f},{east:.8f},{north:.8f}",
        },
        timeout=timeout,
        max_attempts=max_attempts,
        retry_base_delay=retry_base_delay,
    )
    if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise RuntimeError(f"Unexpected Osmose response for bbox={bbox}")
    return data


def split_bbox(
    bbox: tuple[float, float, float, float]
) -> tuple[tuple[float, float, float, float], ...]:
    west, south, east, north = bbox
    mid_lon = (west + east) / 2.0
    mid_lat = (south + north) / 2.0
    return (
        (west, south, mid_lon, mid_lat),
        (mid_lon, south, east, mid_lat),
        (west, mid_lat, mid_lon, north),
        (mid_lon, mid_lat, east, north),
    )


def bbox_key(bbox: tuple[float, float, float, float]) -> str:
    return ",".join(f"{v:.10f}" for v in bbox)


def voltage_values_from_tag(raw: Any) -> list[float]:
    """Return voltage values in volts from an OSM voltage tag."""
    if raw is None:
        return []
    text = str(raw).strip().lower()
    if not text:
        return []

    values: list[float] = []
    for part in re.split(r"[;,/]", text):
        part = part.strip()
        if not part:
            continue
        m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*(kv|v)?", part)
        if not m:
            continue
        value = float(m.group(1))
        unit = m.group(2)
        if unit == "kv":
            value *= 1000.0
        values.append(value)
    return values


def issue_elements(feature: dict[str, Any]) -> list[dict[str, Any]]:
    elems = (feature.get("properties") or {}).get("elems") or []
    return [e for e in elems if isinstance(e, dict)] if isinstance(elems, list) else []


def element_tags(elem: dict[str, Any]) -> dict[str, Any]:
    tags = elem.get("tags")
    return tags if isinstance(tags, dict) else {}


def voltages_of(elems: Iterable[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for elem in elems:
        values.extend(voltage_values_from_tag(element_tags(elem).get("voltage")))
    return values


def line_voltage_values(feature: dict[str, Any]) -> list[float]:
    """Return the line's voltages, using a tagged support only if the line has none."""
    elems = issue_elements(feature)
    values = voltages_of(e for e in elems if element_tags(e).get("power") in LINE_POWER_VALUES)
    if values:
        return values
    return voltages_of(
        e
        for e in elems
        if element_tags(e).get("power") != "substation"
    )


def substation_voltage_values(feature: dict[str, Any]) -> list[float]:
    return voltages_of(
        e for e in issue_elements(feature) if element_tags(e).get("power") == "substation"
    )


def element_ref(elem: dict[str, Any]) -> str | None:
    typ = elem.get("type")
    ident = elem.get("id")
    return f"{typ}/{ident}" if typ is not None and ident is not None else None


def element_refs(feature: dict[str, Any]) -> list[str]:
    return [ref for ref in map(element_ref, issue_elements(feature)) if ref]


def issue_key(feature: dict[str, Any]) -> str:
    props = feature.get("properties") or {}
    uuid = props.get("uuid") or props.get("id") or feature.get("id")
    if uuid is not None:
        return str(uuid)
    return json.dumps(
        [feature.get("geometry"), props.get("item"), props.get("elems")],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def exclusion_reason(
    feature: dict[str, Any],
    issue_type: IssueType,
    osm_elements: dict[str, dict[str, Any] | None],
) -> str | None:
    """Return why an issue is not a real problem on the live grid, or None if it is."""
    line_tags: dict[str, Any] = {}
    for elem in issue_elements(feature):
        ref = element_ref(elem)
        if ref in osm_elements and osm_elements[ref] is None:
            return "deleted_from_osm"
        if element_tags(elem).get("power") in LINE_POWER_VALUES:
            line_tags = osm_elements.get(ref) or element_tags(elem)

    for prefix in LIFECYCLE_PREFIXES:
        if line_tags.get(prefix, "no") != "no" or f"{prefix}:power" in line_tags:
            return "not_in_service"

    if issue_type.exclude_internal_lines and line_tags.get("line") in INTERNAL_LINE_VALUES:
        return "substation_internal_line"

    if issue_type.osmose_class == 7:
        line_v = line_voltage_values(feature)
        substation_v = substation_voltage_values(feature)
        if line_v and substation_v and max(line_v) <= max(substation_v):
            return "voltages_match_numerically"
    return None


def make_josm_url(point: tuple[float, float] | None, refs: list[str]) -> str | None:
    """JOSM remote-control link that loads the area around the issue and selects its elements."""
    if point is None:
        return None
    lon, lat = point
    kinds = {"N": "node", "W": "way", "R": "relation"}
    select = ",".join(
        f"{kinds[r[0]]}{r[2:]}" for r in refs if r[:1] in kinds and r[1:2] == "/"
    )
    url = (
        f"{JOSM_REMOTE_URL}?left={lon - JOSM_ZOOM_DEG:.7f}&right={lon + JOSM_ZOOM_DEG:.7f}"
        f"&bottom={lat - JOSM_ZOOM_DEG:.7f}&top={lat + JOSM_ZOOM_DEG:.7f}"
    )
    return f"{url}&select={select}" if select else url


def make_output_feature(
    feature: dict[str, Any],
    voltages_v: list[float],
    issue_type: IssueType,
    osm_verified: bool,
    reason: str | None,
) -> dict[str, Any]:
    src_props = feature.get("properties") or {}
    uuid = src_props.get("uuid") or src_props.get("id") or feature.get("id")
    geometry = feature.get("geometry")
    refs = element_refs(feature)
    max_v = max(voltages_v) if voltages_v else None

    osmose_url = None
    if geometry and geometry.get("type") == "Point":
        coords = geometry.get("coordinates") or []
        if len(coords) >= 2:
            lon, lat = coords[0], coords[1]
            uuid_fragment = f"&issue_uuid={uuid}" if uuid else ""
            osmose_url = (
                f"{OSMOSE_MAP_URL}#loc=17/{lat}/{lon}&item={ITEM}&class={issue_type.osmose_class}"
                f"&level=1%2C2%2C3{uuid_fragment}"
            )

    props: dict[str, Any] = {
        "osmose_uuid": uuid,
        "osmose_item": ITEM,
        "osmose_class": issue_type.osmose_class,
        "issue": issue_type.title,
        "max_voltage_v": None if max_v is None else int(round(max_v)),
        "max_voltage_kv": None if max_v is None else round(max_v / 1000.0, 3),
        "voltages_v": ";".join(str(int(round(v))) for v in sorted(set(voltages_v))),
        "osm_elements": ";".join(refs),
        "country": src_props.get("country"),
        "osmose_timestamp": src_props.get("timestamp"),
        "osm_verified": osm_verified,
    }
    if issue_type.osmose_class == 7:
        substation_v = substation_voltage_values(feature)
        props["substation_voltages_v"] = ";".join(
            str(int(round(v))) for v in sorted(set(substation_v))
        )
        props["substation_max_voltage_kv"] = (
            round(max(substation_v) / 1000.0, 3) if substation_v else None
        )
        props["voltage_gap_kv"] = (
            round((max_v - max(substation_v)) / 1000.0, 3)
            if substation_v and max_v is not None
            else None
        )
    if reason:
        props["exclusion_reason"] = reason
    if osmose_url:
        props["osmose_url"] = osmose_url
    josm_url = make_josm_url(point_of(feature), refs)
    if josm_url:
        props["josm_url"] = josm_url

    return {"type": "Feature", "geometry": geometry, "properties": props}


def world_tiles(tile_deg: float) -> Iterable[tuple[float, float, float, float]]:
    if not (0 < tile_deg <= 180):
        raise ValueError("--tile-deg must be > 0 and <= 180")

    lon = -180.0
    while lon < 180.0 - 1e-12:
        east = min(180.0, lon + tile_deg)
        lat = -90.0
        while lat < 90.0 - 1e-12:
            north = min(90.0, lat + tile_deg)
            yield (lon, lat, east, north)
            lat = north
        lon = east


def init_state_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tiles (
            key TEXT PRIMARY KEY,
            west REAL NOT NULL,
            south REAL NOT NULL,
            east REAL NOT NULL,
            north REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'split', 'done'))
        );
        CREATE TABLE IF NOT EXISTS issues (
            key TEXT PRIMARY KEY,
            feature_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS osm_elements (
            ref TEXT PRIMARY KEY,
            tags_json TEXT
        );
        """
    )
    conn.commit()


def expected_metadata(args: argparse.Namespace) -> dict[str, str]:
    issue_type = ISSUE_TYPES[args.issue]
    common = {
        "state_version": str(STATE_VERSION),
        "tile_deg": format(args.tile_deg, ".12g"),
        "min_tile_deg": format(args.min_tile_deg, ".12g"),
    }
    if issue_type.source == "overpass":
        return {
            **common,
            "source": "overpass",
            "check": args.issue,
            "prefilter_v": str(prefilter_voltage_v(args.min_voltage_kv)),
        }
    return {
        **common,
        "item": str(ITEM),
        "class": str(issue_type.osmose_class),
        "levels": args.levels,
    }


def validate_or_create_metadata(conn: sqlite3.Connection, args: argparse.Namespace) -> bool:
    """Validate checkpoint parameters. Return True if this is a resumed run."""
    expected = expected_metadata(args)
    existing = dict(conn.execute("SELECT key, value FROM metadata"))
    if not existing:
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [*expected.items(), ("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))],
        )
        conn.commit()
        return False

    mismatches = [
        f"{k}: checkpoint={existing.get(k)!r}, current={v!r}"
        for k, v in expected.items()
        if existing.get(k) != v
    ]
    if mismatches:
        raise RuntimeError(
            "Checkpoint parameters do not match this run:\n  "
            + "\n  ".join(mismatches)
            + "\nUse the original arguments, choose a different --state file, or pass --reset-state."
        )
    return True


def get_tile_status(conn: sqlite3.Connection, bbox: tuple[float, float, float, float]) -> str | None:
    row = conn.execute("SELECT status FROM tiles WHERE key = ?", (bbox_key(bbox),)).fetchone()
    return row[0] if row else None


def upsert_tile(
    conn: sqlite3.Connection,
    bbox: tuple[float, float, float, float],
    status: str,
) -> None:
    west, south, east, north = bbox
    conn.execute(
        """
        INSERT INTO tiles(key, west, south, east, north, status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET status=excluded.status
        """,
        (bbox_key(bbox), west, south, east, north, status),
    )


def store_leaf_results(
    conn: sqlite3.Connection,
    bbox: tuple[float, float, float, float],
    features: list[dict[str, Any]],
) -> None:
    """Atomically store all issues from a complete leaf tile and mark it done."""
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO issues(key, feature_json) VALUES (?, ?)",
            [
                (issue_key(f), json.dumps(f, ensure_ascii=False, separators=(",", ":")))
                for f in features
            ],
        )
        upsert_tile(conn, bbox, "done")


def mark_split(
    conn: sqlite3.Connection,
    bbox: tuple[float, float, float, float],
    children: tuple[tuple[float, float, float, float], ...],
) -> None:
    """Persist subdivision before descending, so resume never needs to re-query the parent."""
    with conn:
        upsert_tile(conn, bbox, "split")
        for child in children:
            if get_tile_status(conn, child) is None:
                upsert_tile(conn, child, "pending")


def process_tile(
    conn: sqlite3.Connection,
    session: requests.Session,
    limiter: RateLimiter,
    bbox: tuple[float, float, float, float],
    *,
    osmose_class: int,
    levels: str,
    timeout: float,
    max_attempts: int,
    retry_base_delay: float,
    min_tile_deg: float,
    depth: int = 0,
) -> None:
    status = get_tile_status(conn, bbox)
    if status == "done":
        return
    if status == "split":
        for child in split_bbox(bbox):
            process_tile(
                conn,
                session,
                limiter,
                child,
                osmose_class=osmose_class,
                levels=levels,
                timeout=timeout,
                max_attempts=max_attempts,
                retry_base_delay=retry_base_delay,
                min_tile_deg=min_tile_deg,
                depth=depth + 1,
            )
        return

    data = request_geojson(
        session,
        limiter,
        bbox,
        osmose_class=osmose_class,
        levels=levels,
        timeout=timeout,
        max_attempts=max_attempts,
        retry_base_delay=retry_base_delay,
    )
    features = data["features"]

    if len(features) < API_LIMIT:
        store_leaf_results(conn, bbox, features)
        return

    west, south, east, north = bbox
    width = east - west
    height = north - south
    if width / 2.0 < min_tile_deg or height / 2.0 < min_tile_deg:
        raise RuntimeError(
            "A tile still returned the Osmose API limit of "
            f"{API_LIMIT} issues at bbox={bbox}. Refusing to silently truncate. "
            "Re-run with a smaller --min-tile-deg if necessary."
        )

    print(f"  API limit reached in {bbox}; subdividing (depth {depth + 1})...", file=sys.stderr)
    children = split_bbox(bbox)
    mark_split(conn, bbox, children)
    for child in children:
        process_tile(
            conn,
            session,
            limiter,
            child,
            osmose_class=osmose_class,
            levels=levels,
            timeout=timeout,
            max_attempts=max_attempts,
            retry_base_delay=retry_base_delay,
            min_tile_deg=min_tile_deg,
            depth=depth + 1,
        )


class TileTooLarge(Exception):
    """Overpass gave up on a tile (timeout or memory); it must be subdivided."""


def prefilter_voltage_v(min_voltage_kv: float) -> int:
    """Largest power of ten <= the threshold: Overpass preselects lines by voltage digit count."""
    threshold = int(min_voltage_kv * 1000)
    return 10 ** (len(str(threshold)) - 1) if threshold >= 1 else 1


def overpass_query(
    session: requests.Session,
    limiter: RateLimiter,
    query: str,
    *,
    overpass_url: str,
    description: str,
    timeout: float,
    max_attempts: int,
    retry_base_delay: float,
) -> list[dict[str, Any]]:
    data = request_json(
        session,
        limiter,
        "POST",
        overpass_url,
        description=description,
        data={"data": query},
        timeout=timeout + 60,  # let the server report its own timeout first
        max_attempts=max_attempts,
        retry_base_delay=retry_base_delay,
    )
    remark = str(data.get("remark") or "")
    if "timed out" in remark or "out of memory" in remark:
        raise TileTooLarge(remark)
    if remark or not isinstance(data.get("elements"), list):
        raise RuntimeError(f"Incomplete Overpass response for {description}: {remark}")
    return data["elements"]


def fetch_junctions(
    session: requests.Session,
    limiter: RateLimiter,
    bbox: BBox,
    *,
    prefilter_v: int,
    overpass_url: str,
    timeout: float,
    max_attempts: int,
    retry_base_delay: float,
) -> list[dict[str, Any]]:
    """Return nodes inside bbox where power ways with differing voltage tags meet."""
    west, south, east, north = bbox
    digits = len(str(prefilter_v))
    common = dict(
        overpass_url=overpass_url,
        timeout=timeout,
        max_attempts=max_attempts,
        retry_base_delay=retry_base_delay,
    )
    ways = overpass_query(
        session,
        limiter,
        f"[out:json][timeout:{int(timeout)}][bbox:{south},{west},{north},{east}];"
        f'way[power~"^(line|cable)$"][voltage~"[0-9]{{{digits}}}"]->.hv;'
        "node(w.hv)->.a;"
        'way(bn.a)[power~"^(line|minor_line|cable)$"];'
        "out body;",
        description=f"Overpass power lines in bbox={bbox}",
        **common,
    )

    ways_by_node: dict[int, dict[int, dict[str, Any]]] = {}
    for way in ways:
        if way.get("type") != "way":
            continue
        for node_id in set(way.get("nodes") or []):
            ways_by_node.setdefault(node_id, {})[way["id"]] = way

    candidates: dict[int, list[dict[str, Any]]] = {}
    for node_id, node_ways in ways_by_node.items():
        if len(node_ways) < 2:
            continue
        voltage_sets = {
            frozenset(voltage_values_from_tag((w.get("tags") or {}).get("voltage")))
            for w in node_ways.values()
        }
        values = set().union(*voltage_sets)
        if len(voltage_sets) > 1 and values and max(values) >= prefilter_v:
            candidates[node_id] = list(node_ways.values())

    junctions = []
    ids = sorted(candidates)
    for start in range(0, len(ids), OVERPASS_ID_BATCH):
        batch = ids[start : start + OVERPASS_ID_BATCH]
        nodes = overpass_query(
            session,
            limiter,
            f"[out:json][timeout:{int(timeout)}];node(id:{','.join(map(str, batch))});out;",
            description=f"Overpass junction nodes in bbox={bbox}",
            **common,
        )
        for node in nodes:
            lon, lat = node.get("lon"), node.get("lat")
            if lon is None or lat is None or not (west <= lon <= east and south <= lat <= north):
                continue  # handled by the tile that contains it, which sees all its ways
            junctions.append(
                {
                    "node": {
                        "id": node["id"],
                        "lon": lon,
                        "lat": lat,
                        "tags": node.get("tags") or {},
                    },
                    "ways": [
                        {"id": w["id"], "tags": w.get("tags") or {}}
                        for w in sorted(candidates[node["id"]], key=lambda w: w["id"])
                    ],
                }
            )
    return junctions


def process_junction_tile(
    conn: sqlite3.Connection,
    session: requests.Session,
    limiter: RateLimiter,
    bbox: BBox,
    *,
    min_tile_deg: float,
    depth: int = 0,
    **fetch_args: Any,
) -> None:
    status = get_tile_status(conn, bbox)
    if status == "done":
        return
    if status != "split":
        try:
            junctions = fetch_junctions(session, limiter, bbox, **fetch_args)
        except TileTooLarge as exc:
            if (bbox[2] - bbox[0]) / 2.0 < min_tile_deg or (bbox[3] - bbox[1]) / 2.0 < min_tile_deg:
                raise RuntimeError(
                    f"Overpass still fails for bbox={bbox} ({exc}). "
                    "Re-run with a smaller --min-tile-deg or a larger --timeout."
                ) from exc
            print(f"  Overpass gave up on {bbox}; subdividing (depth {depth + 1})...", file=sys.stderr)
            mark_split(conn, bbox, split_bbox(bbox))
        else:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO issues(key, feature_json) VALUES (?, ?)",
                    [
                        (f"N/{j['node']['id']}", json.dumps(j, ensure_ascii=False, separators=(",", ":")))
                        for j in junctions
                    ],
                )
                upsert_tile(conn, bbox, "done")
            return
    for child in split_bbox(bbox):
        process_junction_tile(
            conn, session, limiter, child, min_tile_deg=min_tile_deg, depth=depth + 1, **fetch_args
        )


def junction_feature(junction: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a stored junction into an output feature, or None if the voltages agree."""
    node = junction["node"]
    # voltage=0 marks earth-wire or unused circuits, which are not a voltage to match.
    way_voltages = {
        f"W/{w['id']}": sorted(
            {int(round(v)) for v in voltage_values_from_tag(w["tags"].get("voltage")) if v > 0}
        )
        for w in junction["ways"]
    }
    sets = [frozenset(v) for v in way_voltages.values()]
    if len(set(sets)) < 2:
        return None
    counts = Counter(v for vs in sets for v in vs)
    all_v = sorted(counts)
    unmatched = sorted(v for v, c in counts.items() if c == 1)
    if not all(sets):
        kind = "missing_voltage"
    elif unmatched:
        kind = "voltage_ends"
    else:
        kind = "voltage_split"

    point = (node["lon"], node["lat"])
    node_ref = f"N/{node['id']}"
    refs = [node_ref, *way_voltages]
    props: dict[str, Any] = {
        "issue_id": f"junction/{node_ref}",
        "issue": "Connection between different voltages",
        "kind": kind,
        "kind_description": JUNCTION_KINDS[kind],
        "max_voltage_v": max(all_v) if all_v else None,
        "max_voltage_kv": round(max(all_v) / 1000.0, 3) if all_v else None,
        "voltages_v": ";".join(map(str, all_v)),
        "unmatched_voltages_v": ";".join(map(str, unmatched)),
        "voltage_gap_kv": round((max(all_v) - min(all_v)) / 1000.0, 3) if all_v else None,
        "line_voltages": " | ".join(
            f"W/{w['id']}={w['tags'].get('voltage', 'none')}" for w in junction["ways"]
        ),
        "line_count": len(way_voltages),
        "osm_elements": ";".join(refs),
        "country": None,
        "osm_verified": True,
    }
    for key in JUNCTION_NODE_TAGS:
        props[f"node_{key.replace(':', '_')}"] = node["tags"].get(key)
    josm_url = make_josm_url(point, refs)
    if josm_url:
        props["josm_url"] = josm_url
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [node["lon"], node["lat"]]},
        "properties": props,
    }


def select_junctions(
    conn: sqlite3.Connection,
    threshold_v: float,
    region: Region,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    stats: Counter[str] = Counter()
    features = []
    for (junction_json,) in conn.execute("SELECT feature_json FROM issues ORDER BY key"):
        stats["total"] += 1
        feature = junction_feature(json.loads(junction_json))
        if feature is None:
            continue
        if not region.contains(point_of(feature), None):
            stats["outside_region"] += 1
            continue
        if (feature["properties"]["max_voltage_v"] or 0) < threshold_v:
            stats["below_threshold"] += 1
            continue
        stats["kept"] += 1
        stats[f"kind:{feature['properties']['kind']}"] += 1
        features.append(feature)
    return features, stats


def select_issues(
    conn: sqlite3.Connection,
    threshold_v: float,
    include_missing_voltage: bool,
    region: Region,
) -> tuple[list[tuple[dict[str, Any], list[float]]], Counter[str]]:
    """Apply the voltage threshold and drop repeats of the same OSM elements."""
    stats: Counter[str] = Counter()
    selected: list[tuple[dict[str, Any], list[float]]] = []
    seen: set[tuple[str, ...]] = set()
    for key, feature_json in conn.execute("SELECT key, feature_json FROM issues ORDER BY key"):
        feature = json.loads(feature_json)
        stats["total"] += 1
        if not region.contains(point_of(feature), (feature.get("properties") or {}).get("country")):
            stats["outside_region"] += 1
            continue
        voltages_v = line_voltage_values(feature)
        if not voltages_v:
            stats["missing_voltage"] += 1
            if not include_missing_voltage:
                continue
        elif max(voltages_v) < threshold_v:
            stats["below_threshold"] += 1
            continue

        identity = tuple(sorted(element_refs(feature))) or (key,)
        if identity in seen:
            stats["duplicate"] += 1
            continue
        seen.add(identity)
        selected.append((feature, voltages_v))
    return selected, stats


def lookup_osm_elements(
    conn: sqlite3.Connection,
    session: requests.Session,
    limiter: RateLimiter,
    refs: set[str],
    *,
    overpass_url: str,
    timeout: float,
    max_attempts: int,
    retry_base_delay: float,
) -> None:
    """Cache current OSM tags for refs. Elements Overpass doesn't return are stored as deleted."""
    cached = {row[0] for row in conn.execute("SELECT ref FROM osm_elements")}
    missing = sorted(refs - cached)
    for start in range(0, len(missing), OVERPASS_BATCH_SIZE):
        batch = missing[start : start + OVERPASS_BATCH_SIZE]
        nodes = [r[2:] for r in batch if r.startswith("N/")]
        ways = [r[2:] for r in batch if r.startswith("W/")]
        relations = [r[2:] for r in batch if r.startswith("R/")]
        statements = "".join(
            f"{kind}(id:{','.join(ids)});"
            for kind, ids in (("node", nodes), ("way", ways), ("relation", relations))
            if ids
        )
        query = f"[out:json][timeout:{int(timeout)}];({statements});out tags;"
        print(
            f"Checking OSM elements {start + 1}-{start + len(batch)} of {len(missing)}...",
            file=sys.stderr,
        )
        try:
            data = request_json(
                session,
                limiter,
                "POST",
                overpass_url,
                description=f"Overpass batch at {start}",
                data={"data": query},
                timeout=timeout,
                max_attempts=max_attempts,
                retry_base_delay=retry_base_delay,
            )
            if data.get("remark") or not isinstance(data.get("elements"), list):
                raise RuntimeError(f"Incomplete Overpass response: {data.get('remark')}")
        except RuntimeError as exc:
            # A failed lookup must not look like a deleted element, so leave it uncached.
            print(f"  WARNING: {exc}; these issues stay unverified.", file=sys.stderr)
            continue

        prefixes = {"node": "N", "way": "W", "relation": "R"}
        found = {
            f"{prefixes[e['type']]}/{e['id']}": e.get("tags") or {} for e in data["elements"]
        }
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO osm_elements(ref, tags_json) VALUES (?, ?)",
                [
                    (r, json.dumps(found[r], ensure_ascii=False) if r in found else None)
                    for r in batch
                ],
            )


def load_osm_elements(conn: sqlite3.Connection, refs: set[str]) -> dict[str, dict[str, Any] | None]:
    """Return cached elements for refs: their tags, or None for a deleted element."""
    return {
        ref: json.loads(tags_json) if tags_json is not None else None
        for ref, tags_json in conn.execute("SELECT ref, tags_json FROM osm_elements")
        if ref in refs
    }


def build_output_features(
    selected: list[tuple[dict[str, Any], list[float]]],
    issue_type: IssueType,
    osm_elements: dict[str, dict[str, Any] | None],
    keep_excluded: bool,
    stats: Counter[str],
) -> list[dict[str, Any]]:
    features = []
    for feature, voltages_v in selected:
        reason = exclusion_reason(feature, issue_type, osm_elements)
        if reason:
            stats[f"excluded:{reason}"] += 1
            if not keep_excluded:
                continue
        verified = all(ref in osm_elements for ref in element_refs(feature))
        stats["kept" if not reason else "kept_excluded"] += 1
        if not verified:
            stats["unverified"] += 1
        features.append(make_output_feature(feature, voltages_v, issue_type, verified, reason))
    return features


def rank_features(features: list[dict[str, Any]]) -> None:
    """Sort by priority (highest voltage, then largest voltage gap) and number the result."""

    def key(feature: dict[str, Any]) -> tuple[Any, ...]:
        props = feature["properties"]
        return (
            "exclusion_reason" in props,
            JUNCTION_KIND_ORDER.get(props.get("kind"), 0),
            -(props.get("max_voltage_v") or 0),
            -(props.get("voltage_gap_kv") or 0),
            props.get("country") or "",
            props.get("osmose_uuid") or props.get("issue_id") or "",
        )

    features.sort(key=key)
    for rank, feature in enumerate(features, 1):
        feature["properties"]["priority"] = rank


def feature_identity(feature: dict[str, Any]) -> str:
    props = feature.get("properties") or {}
    return str(props.get("issue_id") or props.get("osm_elements") or props.get("osmose_uuid") or "")


def compare_with_previous(
    features: list[dict[str, Any]],
    previous_path: Path,
    region: Region,
) -> list[dict[str, Any]]:
    """Label features as new/open and return earlier features that are no longer present."""
    previous = json.loads(previous_path.read_text(encoding="utf-8")).get("features") or []
    previous = [
        f
        for f in previous
        if "exclusion_reason" not in (f.get("properties") or {})
        and region.contains(point_of(f), (f.get("properties") or {}).get("country"))
    ]
    previous_ids = {feature_identity(f) for f in previous}
    current_ids = set()
    for feature in features:
        ident = feature_identity(feature)
        current_ids.add(ident)
        feature["properties"]["change"] = "open" if ident in previous_ids else "new"
    resolved = [f for f in previous if feature_identity(f) not in current_ids]
    for feature in resolved:
        feature["properties"]["change"] = "resolved"
    return resolved


# Osmose region names start with the country; these words begin multi-word country names.
MULTI_WORD_COUNTRY_STARTS = {
    "bosnia", "burkina", "cape", "central", "costa", "czech", "dominican", "east", "el",
    "equatorial", "new", "north", "papua", "puerto", "saudi", "sierra", "south", "sri",
    "trinidad", "united",
}


def country_of(region: Any) -> str:
    parts = str(region or "unknown").lower().split("_")
    take = 2 if parts[0] in MULTI_WORD_COUNTRY_STARTS and len(parts) > 1 else 1
    return "_".join(parts[:take])


def print_change_summary(features: list[dict[str, Any]], resolved: list[dict[str, Any]]) -> None:
    by_country: dict[str, Counter[str]] = {}
    for feature in [*features, *resolved]:
        props = feature["properties"]
        if props.get("exclusion_reason"):
            continue
        country = country_of(props.get("country"))
        by_country.setdefault(country, Counter())[props["change"]] += 1
    totals = sum(by_country.values(), Counter())
    print(
        f"Changes: {totals['new']} new, {totals['open']} still open, "
        f"{totals['resolved']} resolved.",
        file=sys.stderr,
    )
    rows = sorted(by_country.items(), key=lambda kv: -sum(kv[1].values()))
    if [country for country, _ in rows] == ["unknown"]:
        return
    for country, counts in rows[:20]:
        print(
            f"  {country:<20} new {counts['new']:>5}  open {counts['open']:>5}  "
            f"resolved {counts['resolved']:>5}",
            file=sys.stderr,
        )
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more countries", file=sys.stderr)


def write_maproulette(path: Path, features: list[dict[str, Any]]) -> None:
    """Line-delimited GeoJSON: one FeatureCollection per line, each becoming one task."""
    lines = []
    for feature in features:
        if feature["properties"].get("exclusion_reason"):
            continue
        task = dict(feature)
        task["id"] = feature["properties"].get("osmose_uuid") or feature["properties"].get("issue_id")
        lines.append(
            json.dumps(
                {"type": "FeatureCollection", "features": [task]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    temp.replace(path)


def write_geojson(
    output: Path,
    features: list[dict[str, Any]],
    issue_type: IssueType,
    min_voltage_kv: float,
    name_suffix: str = "",
) -> None:
    collection = {
        "type": "FeatureCollection",
        "name": f"Osmose {issue_type.title} >= {min_voltage_kv:g} kV{name_suffix}",
        "features": features,
    }
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(output)


def state_counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    total = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    done_tiles = conn.execute("SELECT COUNT(*) FROM tiles WHERE status='done'").fetchone()[0]
    split_tiles = conn.execute("SELECT COUNT(*) FROM tiles WHERE status='split'").fetchone()[0]
    return total, done_tiles, split_tiles


def collect_osmose_features(
    conn: sqlite3.Connection,
    session: requests.Session,
    args: argparse.Namespace,
    issue_type: IssueType,
    threshold_v: float,
    region: Region,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    selected, stats = select_issues(conn, threshold_v, args.include_missing_voltage, region)
    refs = {ref for feature, _ in selected for ref in element_refs(feature)}
    if not args.no_osm_check:
        lookup_osm_elements(
            conn,
            session,
            RateLimiter(args.request_delay),
            refs,
            overpass_url=args.overpass_url,
            timeout=args.timeout,
            max_attempts=args.max_attempts,
            retry_base_delay=args.retry_base_delay,
        )
    osm_elements = {} if args.no_osm_check else load_osm_elements(conn, refs)
    features = build_output_features(selected, issue_type, osm_elements, args.keep_excluded, stats)
    return features, stats


def main() -> int:
    args = parse_args()
    if args.request_delay < 0:
        raise SystemExit("--request-delay must be >= 0")
    if args.max_attempts < 1:
        raise SystemExit("--max-attempts must be >= 1")
    if args.retry_base_delay < 0:
        raise SystemExit("--retry-base-delay must be >= 0")

    region = parse_region(args)
    compare_path = Path(args.compare_to) if args.compare_to else None
    if compare_path and not compare_path.is_file():
        raise SystemExit(f"--compare-to file not found: {compare_path}")

    issue_type = ISSUE_TYPES[args.issue]
    is_overpass = issue_type.source == "overpass"
    if is_overpass and region.countries:
        raise SystemExit("--country needs Osmose country data; use --bbox for different-voltages")
    threshold_v = args.min_voltage_kv * 1000.0
    prefilter_v = prefilter_voltage_v(args.min_voltage_kv)
    region_suffix = "".join(f"_{c}" for c in region.countries) + ("_bbox" if region.bbox else "")
    output = Path(
        args.output
        or f"{issue_type.file_prefix}_{issue_type.file_stem}_{args.min_voltage_kv:g}kv_and_above"
        f"{region_suffix}.geojson"
    )
    # The Overpass preselection depends on the threshold's order of magnitude, so it is in the name.
    state_stem = issue_type.file_stem + (f"_{prefilter_v / 1000:g}kv" if is_overpass else "")
    state_path = (
        Path(args.state)
        if args.state
        else output.parent / f"{issue_type.file_prefix}_{state_stem}.state.sqlite"
    )

    if args.reset_state and state_path.exists():
        state_path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(state_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

    conn = sqlite3.connect(state_path)
    init_state_db(conn)
    resumed = validate_or_create_metadata(conn, args)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "powerQAtracer/3.0 "
                "(OpenStreetMap QA data export; contact via open-energy-transition.org)"
            )
        }
    )
    limiter = RateLimiter(args.request_delay)

    tiles = [t for t in world_tiles(args.tile_deg) if region.intersects_tile(t)]
    total_unique, done_tiles, split_tiles = state_counts(conn)
    if resumed:
        created_at = conn.execute("SELECT value FROM metadata WHERE key='created_at'").fetchone()[0]
        print(
            f"Reusing {state_path} from {created_at}: {done_tiles} completed leaf tiles, "
            f"{split_tiles} remembered subdivisions, {total_unique} unique issues already stored. "
            "Use --reset-state to download fresh data.",
            file=sys.stderr,
        )
    if is_overpass:
        print(
            f"Querying Overpass for power lines >= {prefilter_v / 1000:g} kV and their junctions "
            f"using {len(tiles)} initial tiles. Minimum request interval: {args.request_delay:g}s.",
            file=sys.stderr,
        )
    else:
        print(
            f"Querying Osmose item {ITEM}, class {issue_type.osmose_class} using {len(tiles)} "
            f"initial world tiles. Minimum request interval: {args.request_delay:g}s.",
            file=sys.stderr,
        )

    try:
        for idx, bbox in enumerate(tiles, 1):
            status = get_tile_status(conn, bbox)
            if idx == 1 or idx % 25 == 0 or idx == len(tiles):
                suffix = f" [{status}]" if status else ""
                print(f"Tile {idx}/{len(tiles)}: {bbox}{suffix}", file=sys.stderr)
            if is_overpass:
                process_junction_tile(
                    conn,
                    session,
                    limiter,
                    bbox,
                    min_tile_deg=args.min_tile_deg,
                    prefilter_v=prefilter_v,
                    overpass_url=args.overpass_url,
                    timeout=args.timeout,
                    max_attempts=args.max_attempts,
                    retry_base_delay=args.retry_base_delay,
                )
                continue
            process_tile(
                conn,
                session,
                limiter,
                bbox,
                osmose_class=issue_type.osmose_class,
                levels=args.levels,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                retry_base_delay=args.retry_base_delay,
                min_tile_deg=args.min_tile_deg,
            )
    except KeyboardInterrupt:
        total_unique, done_tiles, _ = state_counts(conn)
        print(
            f"\nInterrupted. Progress is saved in {state_path}. "
            f"Run the same command again to resume ({done_tiles} leaf tiles complete, "
            f"{total_unique} unique issues stored).",
            file=sys.stderr,
        )
        conn.close()
        return 130
    except Exception:
        print(
            f"\nDownload stopped. Progress is saved in {state_path}; run the same command again "
            "to resume.",
            file=sys.stderr,
        )
        conn.close()
        raise

    if is_overpass:
        features, stats = select_junctions(conn, threshold_v, region)
    else:
        features, stats = collect_osmose_features(
            conn, session, args, issue_type, threshold_v, region
        )
    rank_features(features)
    resolved: list[dict[str, Any]] = []
    resolved_path = output.with_name(f"{output.stem}_resolved.geojson")
    if compare_path:
        resolved = compare_with_previous(features, compare_path, region)
    write_geojson(output, features, issue_type, args.min_voltage_kv)
    if compare_path:
        write_geojson(resolved_path, resolved, issue_type, args.min_voltage_kv, " (resolved)")
    if args.maproulette:
        write_maproulette(Path(args.maproulette), features)
    conn.close()

    if is_overpass:
        print(
            f"Done. {stats['total']} junctions with differing voltages stored; "
            f"{stats['kept']} kept at >= {args.min_voltage_kv:g} kV "
            f"(below threshold: {stats['below_threshold']}).",
            file=sys.stderr,
        )
        for kind in JUNCTION_KINDS:
            print(f"  {kind}: {stats[f'kind:{kind}']}", file=sys.stderr)
    else:
        print(
            f"Done. {stats['total']} unique open class {issue_type.osmose_class} issues stored; "
            f"{stats['kept']} kept at >= {args.min_voltage_kv:g} kV.",
            file=sys.stderr,
        )
        print(
            f"  Below threshold: {stats['below_threshold']}. "
            f"No parseable voltage: {stats['missing_voltage']}"
            f"{' (included)' if args.include_missing_voltage else ' (excluded)'}. "
            f"Duplicates: {stats['duplicate']}.",
            file=sys.stderr,
        )
    for name, count in sorted(stats.items()):
        if name.startswith("excluded:"):
            kept_note = " (kept and labelled)" if args.keep_excluded else ""
            print(f"  Excluded as {name.split(':', 1)[1]}: {count}{kept_note}.", file=sys.stderr)
    if stats["unverified"]:
        print(f"  Not checked against current OSM data: {stats['unverified']}.", file=sys.stderr)
    if stats["outside_region"]:
        print(f"  Outside --bbox/--country: {stats['outside_region']}.", file=sys.stderr)
    if compare_path:
        print_change_summary(features, resolved)
        print(f"Resolved issues: {resolved_path}", file=sys.stderr)
    if args.maproulette:
        print(f"MapRoulette tasks: {args.maproulette}", file=sys.stderr)
    print(f"GeoJSON: {output}", file=sys.stderr)
    print(f"Checkpoint: {state_path}", file=sys.stderr)
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

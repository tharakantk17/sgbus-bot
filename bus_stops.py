import math
import sqlite3
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

DB_PATH = "data/busstops.db"
LTA_STOPS_URL = "https://datamall2.mytransport.sg/ltaodataservice/BusStops"
LTA_ROUTES_URL = "https://datamall2.mytransport.sg/ltaodataservice/BusRoutes"
NEARBY_RADIUS_M = 800


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS bus_stops (
                code        TEXT PRIMARY KEY,
                description TEXT,
                road_name   TEXT,
                latitude    REAL,
                longitude   REAL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS bus_routes (
                service_no    TEXT,
                direction     INTEGER,
                stop_seq      INTEGER,
                bus_stop_code TEXT,
                distance      REAL,
                PRIMARY KEY (service_no, direction, stop_seq)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                full_name  TEXT,
                first_seen TEXT NOT NULL,
                last_seen  TEXT NOT NULL,
                msg_count  INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id    INTEGER PRIMARY KEY,
                blocked_at TEXT NOT NULL
            )
        """)


async def fetch_and_store(api_key: str) -> int:
    headers = {"AccountKey": api_key}
    stops = []
    skip = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await client.get(LTA_STOPS_URL, headers=headers, params={"$skip": skip})
            r.raise_for_status()
            batch = r.json().get("value", [])
            if not batch:
                break
            stops.extend(batch)
            skip += 500

    with _conn() as con:
        con.executemany(
            "INSERT OR REPLACE INTO bus_stops VALUES (?, ?, ?, ?, ?)",
            [
                (
                    s["BusStopCode"],
                    s["Description"],
                    s["RoadName"],
                    s["Latitude"],
                    s["Longitude"],
                )
                for s in stops
            ],
        )
    logger.info("Stored %d bus stops", len(stops))
    return len(stops)


async def fetch_and_store_route(api_key: str, service_no: str) -> None:
    headers = {"AccountKey": api_key}
    records = []
    skip = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            r = await client.get(
                LTA_ROUTES_URL,
                headers=headers,
                params={"ServiceNo": service_no, "$skip": skip},
            )
            r.raise_for_status()
            batch = r.json().get("value", [])
            if not batch:
                break
            records.extend(s for s in batch if s["ServiceNo"] == service_no)
            if len(batch) < 500:
                break
            skip += 500

    with _conn() as con:
        con.execute("DELETE FROM bus_routes WHERE service_no = ?", (service_no,))
        con.executemany(
            "INSERT OR REPLACE INTO bus_routes VALUES (?, ?, ?, ?, ?)",
            [
                (
                    s["ServiceNo"],
                    s["Direction"],
                    s["StopSequence"],
                    s["BusStopCode"],
                    s["Distance"],
                )
                for s in records
            ],
        )
    logger.info("Stored %d route stops for service %s", len(records), service_no)


def clear_route_cache() -> None:
    with _conn() as con:
        con.execute("DELETE FROM bus_routes")


async def ensure_route_loaded(api_key: str, service_no: str) -> None:
    with _conn() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM bus_routes WHERE service_no = ?", (service_no,)
        ).fetchone()[0]
    if count == 0:
        logger.info("Route cache miss for %s — fetching from LTA...", service_no)
        await fetch_and_store_route(api_key, service_no)


def get_route_for_stop(service_no: str, bus_stop_code: str) -> list[tuple[int, list[dict]]]:
    """Return [(direction, [stops])] for every direction that passes through this stop."""
    with _conn() as con:
        directions = con.execute(
            """SELECT DISTINCT direction FROM bus_routes
               WHERE service_no = ? AND bus_stop_code = ?
               ORDER BY direction""",
            (service_no, bus_stop_code),
        ).fetchall()

        result = []
        for (direction,) in directions:
            rows = con.execute(
                """SELECT stop_seq, bus_stop_code, distance FROM bus_routes
                   WHERE service_no = ? AND direction = ?
                   ORDER BY stop_seq""",
                (service_no, direction),
            ).fetchall()
            stops = [{"seq": r[0], "code": r[1], "distance": r[2]} for r in rows]
            result.append((direction, stops))
    return result


async def ensure_loaded(api_key: str) -> None:
    init_db()
    with _conn() as con:
        count = con.execute("SELECT COUNT(*) FROM bus_stops").fetchone()[0]
    if count < 100:
        logger.info("Bus stop DB empty — fetching from LTA...")
        await fetch_and_store(api_key)


def get_stop_info(code: str) -> tuple[str, str]:
    with _conn() as con:
        row = con.execute(
            "SELECT description, road_name FROM bus_stops WHERE code = ?", (code,)
        ).fetchone()
    return (row[0], row[1]) if row else ("", "")


def stop_exists(code: str) -> bool:
    with _conn() as con:
        row = con.execute("SELECT 1 FROM bus_stops WHERE code = ?", (code,)).fetchone()
    return row is not None


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_nearby(lat: float, lon: float, limit: int | None = None) -> list[dict]:
    """Return stops within NEARBY_RADIUS_M sorted by distance, optionally capped at limit."""
    dlat = NEARBY_RADIUS_M / 111_000
    dlon = NEARBY_RADIUS_M / (111_000 * math.cos(math.radians(lat)))
    with _conn() as con:
        rows = con.execute(
            """SELECT code, description, road_name, latitude, longitude
               FROM bus_stops
               WHERE latitude BETWEEN ? AND ? AND longitude BETWEEN ? AND ?""",
            (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
        ).fetchall()

    results = []
    for code, desc, road, slat, slon in rows:
        if slat and slon:
            dist = _haversine(lat, lon, slat, slon)
            if dist <= NEARBY_RADIUS_M:
                results.append({
                    "code": code,
                    "description": desc,
                    "road_name": road,
                    "distance": dist,
                })
    results.sort(key=lambda x: x["distance"])
    return results[:limit] if limit is not None else results


# ---------------------------------------------------------------------------
# User tracking & blocking
# ---------------------------------------------------------------------------

def record_user(user_id: int, username: str | None, full_name: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            """INSERT INTO users (user_id, username, full_name, first_seen, last_seen, msg_count)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(user_id) DO UPDATE SET
                   username  = excluded.username,
                   full_name = excluded.full_name,
                   last_seen = excluded.last_seen,
                   msg_count = msg_count + 1""",
            (user_id, username, full_name, now, now),
        )


def is_blocked(user_id: int) -> bool:
    with _conn() as con:
        return con.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
        ).fetchone() is not None


def block_user(user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO blocked_users VALUES (?, ?)", (user_id, now)
        )


def unblock_user(user_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))


_ADMIN_PAGE_SIZE = 6


def get_users(page: int = 0) -> list[dict]:
    offset = page * _ADMIN_PAGE_SIZE
    with _conn() as con:
        rows = con.execute(
            """SELECT u.user_id, u.username, u.full_name, u.last_seen, u.msg_count,
                      (SELECT 1 FROM blocked_users b WHERE b.user_id = u.user_id) IS NOT NULL
               FROM users u
               ORDER BY u.last_seen DESC
               LIMIT ? OFFSET ?""",
            (_ADMIN_PAGE_SIZE, offset),
        ).fetchall()
    return [
        {
            "id": r[0], "username": r[1], "full_name": r[2],
            "last_seen": r[3], "msg_count": r[4], "blocked": bool(r[5]),
        }
        for r in rows
    ]


def get_user_count() -> int:
    with _conn() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user(user_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            """SELECT u.user_id, u.username, u.full_name, u.last_seen, u.msg_count,
                      (SELECT 1 FROM blocked_users b WHERE b.user_id = u.user_id) IS NOT NULL
               FROM users u WHERE u.user_id = ?""",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "username": row[1], "full_name": row[2],
        "last_seen": row[3], "msg_count": row[4], "blocked": bool(row[5]),
    }


def get_blocked_users() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT b.user_id, u.username, u.full_name, b.blocked_at
               FROM blocked_users b
               LEFT JOIN users u ON u.user_id = b.user_id
               ORDER BY b.blocked_at DESC""",
        ).fetchall()
    return [
        {"id": r[0], "username": r[1], "full_name": r[2], "blocked_at": r[3]}
        for r in rows
    ]

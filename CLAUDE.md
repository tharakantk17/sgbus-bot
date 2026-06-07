# SG Bus Bot — CLAUDE.md

Telegram bot serving live Singapore bus arrival times, running 24/7 on a Raspberry Pi.

## Stack

| Layer | Detail |
|-------|--------|
| Language | Python 3.11 |
| Telegram library | `python-telegram-bot` v21.9 (async, PTB) |
| HTTP client | `httpx` — shared `AsyncClient` (created in `post_init`, closed in `post_shutdown`) |
| Data source | LTA DataMall API v3 — `/v3/BusArrival`, `BusStops`, `BusRoutes` |
| Local DB | SQLite at `data/busstops.db` — tables: `bus_stops`, `bus_routes`, `users`, `blocked_users` |
| Persistence | `PicklePersistence` at `data/bot_data.pickle` — stores per-user favourites |
| Env vars | `TELEGRAM_BOT_TOKEN`, `LTA_API_KEY`, `ADMIN_USER_IDS` (comma-separated int list) |

## Files

- **`bot.py`** — all Telegram handlers, formatting, admin panel, app lifecycle
- **`bus_stops.py`** — SQLite schema, LTA API fetchers, sync DB helpers (all called via `asyncio.to_thread` from async handlers)

## Async / SQLite rule

All SQLite functions in `bus_stops.py` are **synchronous**. Every call from an async handler in `bot.py` **must** be wrapped:

```python
result = await asyncio.to_thread(bus_stops.some_fn, arg1, arg2)
```

The only exception is `_format_route_section`, which is already dispatched via `asyncio.to_thread` and may call sync DB functions directly inside the thread.

## Key design points

- **Shared HTTP client** — `_http_client` is a module-level `httpx.AsyncClient`. Do not create per-call clients.
- **Rate limiting** — `_refresh_cooldowns` dict + `_REFRESH_COOLDOWN = 3.0 s` guards the refresh/fav callback against rapid taps.
- **Favourite key format** — `"{bus_stop_code}:{service_no}"` (or just `bus_stop_code` with no colon if no service filter). Use `_split_fav_key(key)` to unpack; never split manually.
- **Callback data encoding** — favourites encode the key as `bus_stop_code-service_no` in callback_data (`:` → `-`). Bus stop codes are 5-digit numbers; service numbers never contain `-`, so this is safe.
- **User tracking** — `track_and_block` runs in handler group `-1` (before everything). It checks `blocked_users` and upserts into `users` on every message and callback.
- **Admin gate** — `ADMIN_USER_IDS` is a `set[int]`. Empty set = no admin access. `refreshstops` requires admin; `/admin` silently returns if caller not in set.
- **LTA API pagination** — both `fetch_and_store` (stops) and `fetch_and_store_route` (routes) paginate with `$skip` in steps of 500 until an empty batch.

## Service management (Raspberry Pi)

```bash
sudo systemctl restart sgbus-bot.service   # restart
sudo systemctl status sgbus-bot.service    # check status
tail -f /sgbus-bot/data/bot.log            # live logs
```

The service file lives at `/etc/systemd/system/sgbus-bot.service`. It auto-restarts on crash (`Restart=always`, `RestartSec=10`).

## Development workflow

```bash
# activate venv
source .venv/bin/activate

# run locally (needs .env with TELEGRAM_BOT_TOKEN and LTA_API_KEY)
python bot.py

# after changes: commit then restart service
sudo systemctl restart sgbus-bot.service
```

## Data notes

- Bus stop DB is seeded from LTA on first boot (if fewer than 100 rows). Refresh manually with `/refreshstops` (admin only).
- Route data is fetched on-demand per service and cached in `bus_routes`. `/refreshstops` also clears the route cache.
- `data/` directory holds the SQLite DB, pickle file, and `bot.log` — do not delete.

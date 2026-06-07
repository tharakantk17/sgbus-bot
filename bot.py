import asyncio
import html
import json
import logging
import os
import random
import time
import zoneinfo
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

import bus_stops

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
LTA_API_KEY = os.environ["LTA_API_KEY"]
LTA_BASE_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"
ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}

_http_client: httpx.AsyncClient | None = None
_refresh_cooldowns: dict[int, float] = {}
_REFRESH_COOLDOWN = 3.0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📍 Share Location", request_location=True)],
        [KeyboardButton("⭐ My Favourites"), KeyboardButton("📊 Dashboard")],
        [KeyboardButton("❓ Help")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

BOOT_QUIPS = [
    "All systems nominal.",
    "Good to have you back, sir.",
    "Ready when you are.",
    "Standing by.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _h(text: str) -> str:
    return html.escape(str(text))


def _minutes_away(iso_time: str) -> str:
    if not iso_time:
        return "–"
    try:
        arrival = datetime.fromisoformat(iso_time)
        now = datetime.now(timezone.utc)
        diff = int((arrival - now).total_seconds() / 60)
        if diff <= 0:
            return "Arr"
        return f"{diff}m"
    except ValueError:
        return "–"


def _load_icon(load: str) -> str:
    return {"SEA": "🟢", "SDA": "🟡", "LSD": "🔴"}.get(load, "⚪")


def _type_icon(bus_type: str) -> str:
    return "♿" if bus_type == "WAB" else "🚌"


def _format_next_bus(bus: dict) -> str:
    arrival = _minutes_away(bus.get("EstimatedArrival", ""))
    load = _load_icon(bus.get("Load", ""))
    btype = _type_icon(bus.get("Type", ""))
    return f"{load}{btype} <b>{arrival}</b>"


async def _format_services(data: dict) -> str:
    services = data.get("Services", [])
    stop_code = data.get("BusStopCode", "")

    desc, road = await asyncio.to_thread(bus_stops.get_stop_info, stop_code)
    if desc:
        header = f"🚏 <b>{_h(desc)}</b>\n<i>{_h(road)} · Stop {_h(stop_code)}</i>"
    else:
        header = f"🚏 <b>Bus Stop {_h(stop_code)}</b>"

    if not services:
        return header + "\n\n<i>No services currently active at this stop.</i>"

    lines = []
    for svc in services:
        svc_no = _h(svc.get("ServiceNo", "?"))
        next1 = _format_next_bus(svc.get("NextBus", {}))
        next2 = _format_next_bus(svc.get("NextBus2", {}))
        next3 = _format_next_bus(svc.get("NextBus3", {}))
        lines.append(f"<b>{svc_no}</b>  {next1}  {next2}  {next3}")

    now_str = datetime.now(zoneinfo.ZoneInfo("Asia/Singapore")).strftime("%-I:%M %p")

    return (
        f"{header}\n\n"
        + "\n".join(lines)
        + f"\n\n<i>🟢 Seats  ·  🟡 Standing  ·  🔴 Full  ·  ♿ Accessible</i>"
        + f"\n<i>🕐 {now_str}</i>"
    )


def _arrival_keyboard(
    bus_stop_code: str,
    service_no: str,
    services: list[str] | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{bus_stop_code}:{service_no}"),
            InlineKeyboardButton("⭐ Save", callback_data=f"save:{bus_stop_code}:{service_no}"),
        ],
    ]
    if service_no:
        rows.append([
            InlineKeyboardButton("🗺 View Route", callback_data=f"route:{bus_stop_code}:{service_no}"),
        ])
    elif services and len(services) > 1:
        capped = services[:8]
        filter_btns = [
            InlineKeyboardButton(svc, callback_data=f"refresh:{bus_stop_code}:{svc}")
            for svc in capped
        ]
        route_btns = [
            InlineKeyboardButton(f"🗺 {svc}", callback_data=f"route:{bus_stop_code}:{svc}")
            for svc in capped
        ]
        for i in range(0, len(filter_btns), 4):
            rows.append(filter_btns[i : i + 4])
        for i in range(0, len(route_btns), 4):
            rows.append(route_btns[i : i + 4])
    rows.append([InlineKeyboardButton("📋 My Favourites", callback_data="list_favs")])
    return InlineKeyboardMarkup(rows)


def _relative_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        diff = datetime.now(timezone.utc) - dt
        s = int(diff.total_seconds())
        if s < 60:      return "just now"
        if s < 3600:    return f"{s // 60}m ago"
        if s < 86400:   return f"{s // 3600}h ago"
        if s < 604800:  return f"{s // 86400}d ago"
        return f"{s // 604800}w ago"
    except Exception:
        return "?"


def _user_label(u: dict) -> str:
    if u.get("username"):
        return f"@{u['username']}"
    if u.get("full_name"):
        return u["full_name"]
    return f"#{u['id']}"


def _split_fav_key(key: str) -> tuple[str, str]:
    parts = key.split(":", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


async def fetch_arrivals(bus_stop_code: str, service_no: str = "") -> dict:
    params = {"BusStopCode": bus_stop_code}
    if service_no:
        params["ServiceNo"] = service_no
    headers = {"AccountKey": LTA_API_KEY}
    r = await _http_client.get(LTA_BASE_URL, params=params, headers=headers)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    quip = random.choice(BOOT_QUIPS)
    text = (
        f"🤖 <b>S.G. Bus Intelligence Online.</b> {quip}\n\n"
        "I have full access to Singapore's live bus network. How shall we proceed?\n\n"
        "<b>Methods of enquiry</b>\n"
        "📍 Share your location — I'll scan for nearby stops\n"
        "🔢 Transmit a stop code — e.g. <code>83139</code>\n"
        "🚌 Stop + service number — e.g. <code>83139 15</code>\n\n"
        "<b>Commands</b>\n"
        "/stop <code>&lt;code&gt;</code> — query a specific stop\n"
        "/favourites — your saved stops\n"
        "/dashboard — live view of all saved stops\n"
        "/about — full guide &amp; data info\n"
        "/help — quick reference"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 <b>Operational Briefing</b>\n\n"
        "1️⃣ <b>Location scan</b> — tap <i>Share Location</i> below\n"
        "2️⃣ <b>Stop code</b> — transmit <code>83139</code>\n"
        "3️⃣ <b>Targeted query</b> — transmit <code>83139 15</code>\n"
        "4️⃣ <b>Direct command</b> — <code>/stop 83139</code>\n"
        "5️⃣ <b>Dashboard</b> — tap <i>📊 Dashboard</i> or <code>/dashboard</code>\n\n"
        "<b>Capacity indicators</b>\n"
        "🟢 Seats available\n"
        "🟡 Standing room only\n"
        "🔴 Critical capacity\n\n"
        "<b>Locating a stop code</b>\n"
        "The 5-digit identifier is on the physical stop signage, "
        "or share your location for a proximity scan.\n\n"
        "For a full guide including data sources and privacy info, use /about"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "ℹ️ <b>About SG Bus Bot</b>\n\n"
        "Real-time bus arrival times for every stop in Singapore, "
        "powered by the <b>LTA DataMall API</b>.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🚌 <b>How to get arrivals</b>\n"
        "• Tap <i>Share Location</i> — finds nearby stops within 800 m\n"
        "• Send a stop code — e.g. <code>83139</code>\n"
        "• Send stop + service — e.g. <code>83139 65</code> to filter\n"
        "• <code>/stop 83139</code> — same via command\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>Reading the arrival card</b>\n"
        "Each row shows the next 3 buses for a service:\n"
        "<code>65  🟢🚌 2m  🟡🚌 12m  🟢🚌 22m</code>\n\n"
        "🟢 Seats available\n"
        "🟡 Standing room only\n"
        "🔴 Very full (limited boarding)\n"
        "♿ Wheelchair-accessible bus\n"
        "<b>Arr</b> = arriving now  ·  <b>–</b> = no data\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "⭐ <b>Favourites &amp; Dashboard</b>\n"
        "Tap <b>⭐ Save</b> on any arrival to bookmark it. "
        "Use /dashboard (or tap 📊 Dashboard) for a single "
        "refreshable view of all your saved stops at once.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🗺 <b>Route view</b>\n"
        "From any arrival card, tap <b>🗺 [service]</b> to see "
        "the full stop sequence and your current position on the route. "
        "Works for both directions where applicable.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "📡 <b>Data &amp; accuracy</b>\n"
        "Arrival times come directly from LTA DataMall and are "
        "typically <b>1–2 minutes behind real-time</b>. "
        "Bus stop data is cached locally and refreshed periodically.\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🔒 <b>Privacy</b>\n"
        "Only your Telegram user ID and usage count are stored — "
        "solely to keep the service running. "
        "No message content is ever logged or shared."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=MAIN_KEYBOARD)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "I'll need a stop code for that, sir.\nExample: <code>/stop 83139</code>",
            parse_mode="HTML",
        )
        return
    code = args[0].strip()
    if not await asyncio.to_thread(bus_stops.stop_exists, code):
        await update.message.reply_text(
            f"⚠️ Stop <code>{_h(code)}</code> not found in my database. "
            "Check the code or share your location for nearby stops.",
            parse_mode="HTML",
        )
        return
    await _send_arrivals(update, context, code, args[1].strip() if len(args) > 1 else "")


async def favourites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_favourites(update, context)


async def refreshstops_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if ADMIN_USER_IDS and update.effective_user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("🚫 Unauthorised.")
        return
    msg = await update.message.reply_text("⏳ Synchronising bus stop database with LTA…")
    try:
        count = await bus_stops.fetch_and_store(LTA_API_KEY)
        await asyncio.to_thread(bus_stops.clear_route_cache)
        await msg.edit_text(f"✅ Database updated — {count:,} stops registered. Route cache cleared.")
    except Exception as e:
        logger.error("refreshstops error: %s", e)
        await msg.edit_text("❌ Synchronisation failed. Please try again later.")


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

def _nearby_buttons(nearby: list[dict], show: int) -> list:
    buttons = []
    for stop in nearby[:show]:
        dist = stop["distance"]
        dist_str = f"{int(dist)} m" if dist < 1000 else f"{dist / 1000:.1f} km"
        label = f"🚏 {stop['code']}  {stop['description']}  ({dist_str})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"refresh:{stop['code']}:")])
    if len(nearby) > show:
        more = min(5, len(nearby) - show)
        buttons.append([
            InlineKeyboardButton(
                f"Show {more} more stops ↓",
                callback_data=f"more_stops:{show + 5}",
            )
        ])
    return buttons


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loc = update.message.location
    context.user_data["last_location"] = (loc.latitude, loc.longitude)
    nearby = await asyncio.to_thread(bus_stops.get_nearby, loc.latitude, loc.longitude)

    if not nearby:
        await update.message.reply_text(
            "Scan complete. No bus stops detected in your vicinity. "
            "You may transmit a stop code directly."
        )
        return

    buttons = _nearby_buttons(nearby, show=5)
    await update.message.reply_text(
        f"📍 <b>Proximity scan complete.</b> {len(nearby)} stop(s) within range. Select one to query:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if text == "⭐ My Favourites":
        await _send_favourites(update, context)
        return
    if text == "📊 Dashboard":
        await dashboard_command(update, context)
        return
    if text == "❓ Help":
        await help_command(update, context)
        return

    parts = text.split()
    bus_stop_code = parts[0]
    service_no = parts[1] if len(parts) > 1 else ""

    if not bus_stop_code.isdigit() or not (4 <= len(bus_stop_code) <= 5):
        await update.message.reply_text(
            "I require a valid 4–5 digit stop code (e.g. <code>83139</code>). "
            "Alternatively, share your location for a proximity scan.",
            parse_mode="HTML",
        )
        return

    if not await asyncio.to_thread(bus_stops.stop_exists, bus_stop_code):
        await update.message.reply_text(
            f"⚠️ Stop <code>{_h(bus_stop_code)}</code> not found in my database. "
            "Check the code or share your location for nearby stops.",
            parse_mode="HTML",
        )
        return

    await _send_arrivals(update, context, bus_stop_code, service_no)


# ---------------------------------------------------------------------------
# Route map
# ---------------------------------------------------------------------------

_ROUTE_BEFORE = 3
_ROUTE_AFTER = 5


def _format_route_section(
    service_no: str,
    bus_stop_code: str,
    direction: int,
    stops: list[dict],
) -> str:
    current_idx = next((i for i, s in enumerate(stops) if s["code"] == bus_stop_code), None)
    if current_idx is None:
        return f"<i>Route {_h(service_no)} dir {direction}: stop not located.</i>"

    terminal_desc, _ = bus_stops.get_stop_info(stops[-1]["code"])
    terminal = terminal_desc or stops[-1]["code"]
    stops_remaining = len(stops) - 1 - current_idx

    header = (
        f"🗺 <b>Bus {_h(service_no)}</b>  →  <b>{_h(terminal)}</b>\n"
        f"<i>Stop {current_idx + 1} of {len(stops)}  ·  {stops_remaining} to go</i>"
    )

    start_idx = max(0, current_idx - _ROUTE_BEFORE)
    end_idx = min(len(stops), current_idx + _ROUTE_AFTER + 1)

    # Build plain-text timeline (lives inside <pre>, so no HTML tags)
    lines = []
    if start_idx > 0:
        origin_desc, _ = bus_stops.get_stop_info(stops[0]["code"])
        origin = html.escape(origin_desc or stops[0]["code"])
        lines.append(f"  ↑  {start_idx} stop(s) from {origin}")
        lines.append("")

    for i in range(start_idx, end_idx):
        desc, _ = bus_stops.get_stop_info(stops[i]["code"])
        name = html.escape(desc or stops[i]["code"])
        if i == current_idx:
            lines.append(f"  ▶  {name}")
        elif i == len(stops) - 1:
            lines.append(f"  🏁  {name}")
        else:
            lines.append(f"  ○  {name}")

    if end_idx < len(stops):
        lines.append("")
        lines.append(f"  ↓  {len(stops) - end_idx} stop(s) to {html.escape(terminal)}")

    return f"{header}\n\n<pre>" + "\n".join(lines) + "</pre>"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

async def _build_dashboard(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup] | None:
    favourites: dict = context.user_data.get("favourites", {})
    if not favourites:
        return None

    async def safe_fetch(stop: str, svc: str) -> dict | None:
        try:
            return await fetch_arrivals(stop, svc)
        except Exception:
            return None

    keys = list(favourites.keys())
    tasks = [safe_fetch(*_split_fav_key(k)) for k in keys]
    results = await asyncio.gather(*tasks)

    divider = "──────────────────"
    sections = []
    for key, data in zip(keys, results):
        stop, svc = _split_fav_key(key)
        desc, _ = await asyncio.to_thread(bus_stops.get_stop_info, stop)
        stop_name = desc or f"Stop {stop}"

        header = f"🚏 <b>{_h(stop_name)}</b>"
        if svc:
            header += f" · <b>{_h(svc)}</b>"

        if data is None:
            sections.append(f"{header}\n<i>⚠️ Could not fetch</i>")
            continue

        svcs = data.get("Services", [])
        if not svcs:
            sections.append(f"{header}\n<i>No active services</i>")
            continue

        if svc:
            bus = svcs[0]
            row = (
                f"{_format_next_bus(bus.get('NextBus', {}))}"
                f"  {_format_next_bus(bus.get('NextBus2', {}))}"
                f"  {_format_next_bus(bus.get('NextBus3', {}))}"
            )
            sections.append(f"{header}\n{row}")
        else:
            rows = []
            for s in svcs[:6]:
                sno = _h(s.get("ServiceNo", "?"))
                n1 = _format_next_bus(s.get("NextBus", {}))
                n2 = _format_next_bus(s.get("NextBus2", {}))
                rows.append(f"<b>{sno}</b>  {n1}  {n2}")
            sections.append(header + "\n" + "\n".join(rows))

    now_str = datetime.now(zoneinfo.ZoneInfo("Asia/Singapore")).strftime("%-I:%M %p")
    text = (
        f"📊 <b>Dashboard</b>  <i>{now_str}</i>\n\n"
        + f"\n{divider}\n".join(sections)
        + f"\n{divider}\n"
        "🟢 Seats  🟡 Standing  🔴 Limited"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh All", callback_data="dashboard"),
    ]])
    return text, keyboard


async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("favourites"):
        await update.message.reply_text(
            "📊 <b>Dashboard</b>\n\n"
            "Your registry is empty, sir.\n"
            "Query a stop and tap <b>⭐ Save</b> to populate it.",
            parse_mode="HTML",
        )
        return
    msg = await update.message.reply_text("⏳ Compiling dashboard…")
    result = await _build_dashboard(context)
    if result is None:
        await msg.edit_text("Your registry is empty.")
        return
    text, keyboard = result
    await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Shared arrival sender
# ---------------------------------------------------------------------------

async def _send_arrivals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    bus_stop_code: str,
    service_no: str,
) -> None:
    msg = await update.message.reply_text("⏳ Accessing transit systems…")
    try:
        data = await fetch_arrivals(bus_stop_code, service_no)
        services = [svc["ServiceNo"] for svc in data.get("Services", [])]
        await msg.edit_text(
            await _format_services(data),
            parse_mode="HTML",
            reply_markup=_arrival_keyboard(bus_stop_code, service_no, services),
        )
    except httpx.HTTPStatusError as e:
        logger.error("LTA API error: %s", e)
        await msg.edit_text(
            f"❌ I'm afraid the LTA systems returned an error ({e.response.status_code}). "
            "Please verify the stop code and try again."
        )
    except httpx.RequestError as e:
        logger.error("Network error: %s", e)
        await msg.edit_text("❌ I'm experiencing network difficulties. Please try again momentarily.")
    except json.JSONDecodeError as e:
        logger.error("LTA API returned invalid JSON: %s", e)
        await msg.edit_text("❌ I received an unexpected response from LTA. Please try again.")


# ---------------------------------------------------------------------------
# Shared favourites sender
# ---------------------------------------------------------------------------

async def _send_favourites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    favourites: dict = context.user_data.get("favourites", {})
    if not favourites:
        await update.message.reply_text(
            "⭐ <b>Registered Stops</b>\n\n"
            "Your registry is empty, sir.\n"
            "Query a stop and tap <b>⭐ Save</b> to register it.",
            parse_mode="HTML",
        )
        return

    buttons = _favourites_buttons(favourites)
    await update.message.reply_text(
        "⭐ <b>Registered Stops</b>\n\nSelect a stop to query, 🗑 to deregister:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _favourites_buttons(favourites: dict) -> list:
    buttons = []
    for key, label in favourites.items():
        encoded = key.replace(":", "-")
        buttons.append([
            InlineKeyboardButton(f"🚏 {label}", callback_data=f"fav:{encoded}:"),
            InlineKeyboardButton("🗑", callback_data=f"delfav:{encoded}"),
        ])
    return buttons


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("route:"):
        _, bus_stop_code, service_no = data.split(":")
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to arrivals", callback_data=f"refresh:{bus_stop_code}:{service_no}"),
        ]])
        await query.edit_message_text("⏳ Loading route…")
        try:
            await bus_stops.ensure_route_loaded(LTA_API_KEY, service_no)
        except Exception as e:
            logger.error("Route fetch error: %s", e)
            await query.edit_message_text(
                "❌ Could not retrieve route data. Please try again.",
                reply_markup=back_kb,
            )
            return
        route_data = bus_stops.get_route_for_stop(service_no, bus_stop_code)
        if not route_data:
            await query.edit_message_text(
                f"⚠️ No route data found for service <b>{_h(service_no)}</b> at this stop.",
                parse_mode="HTML",
                reply_markup=back_kb,
            )
            return
        divider = "──────────────────"
        sections = await asyncio.gather(*[
            asyncio.to_thread(_format_route_section, service_no, bus_stop_code, direction, stops)
            for direction, stops in route_data
        ])
        await query.edit_message_text(
            f"\n{divider}\n".join(sections),
            parse_mode="HTML",
            reply_markup=back_kb,
        )
        return

    if data == "dashboard":
        result = await _build_dashboard(context)
        if result is None:
            await query.answer("Your registry is empty, sir.", show_alert=True)
            return
        text, keyboard = result
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    if data == "list_favs":
        favourites: dict = context.user_data.get("favourites", {})
        if not favourites:
            await query.answer("Your registry is empty, sir.", show_alert=True)
            return
        await query.edit_message_text(
            "⭐ <b>Registered Stops</b>\n\nSelect a stop to query, 🗑 to deregister:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(_favourites_buttons(favourites)),
        )
        return

    if data.startswith("more_stops:"):
        new_limit = int(data.split(":")[1])
        loc = context.user_data.get("last_location")
        if not loc:
            await query.answer("Location data expired. Share your location again.", show_alert=True)
            return
        lat, lon = loc
        nearby = await asyncio.to_thread(bus_stops.get_nearby, lat, lon)
        buttons = _nearby_buttons(nearby, show=new_limit)
        await query.edit_message_text(
            f"📍 <b>Proximity scan complete.</b> {len(nearby)} stop(s) within range. Select one to query:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("delfav:"):
        encoded = data[len("delfav:"):]
        key = encoded.replace("-", ":", 1)
        favourites: dict = context.user_data.get("favourites", {})
        label = favourites.pop(key, None)
        if label:
            await query.answer(f"Deregistered: {label}")
        if not favourites:
            await query.edit_message_text(
                "⭐ <b>Registered Stops</b>\n\nYour registry is empty, sir.",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(_favourites_buttons(favourites))
            )
        return

    action, bus_stop_code, *rest = data.split(":")
    service_no = rest[0] if rest else ""

    if action in ("refresh", "fav"):
        uid = query.from_user.id
        now = time.monotonic()
        if now - _refresh_cooldowns.get(uid, 0) < _REFRESH_COOLDOWN:
            await query.answer("Please wait a moment.", show_alert=True)
            return
        _refresh_cooldowns[uid] = now
        if action == "fav":
            parts = bus_stop_code.split("-")
            bus_stop_code = parts[0]
            service_no = parts[1] if len(parts) > 1 else ""
        try:
            fetched = await fetch_arrivals(bus_stop_code, service_no)
            services = [svc["ServiceNo"] for svc in fetched.get("Services", [])]
            await query.edit_message_text(
                await _format_services(fetched),
                parse_mode="HTML",
                reply_markup=_arrival_keyboard(bus_stop_code, service_no, services),
            )
        except (httpx.HTTPStatusError, httpx.RequestError, json.JSONDecodeError) as e:
            logger.error("Callback fetch error: %s", e)
            await query.edit_message_text("❌ I was unable to reach the transit systems. Please try again.")

    elif action == "save":
        favourites: dict = context.user_data.setdefault("favourites", {})
        key = f"{bus_stop_code}:{service_no}" if service_no else bus_stop_code
        if key in favourites:
            await query.answer("Already in your registry, sir.", show_alert=True)
        else:
            desc, road = await asyncio.to_thread(bus_stops.get_stop_info, bus_stop_code)
            label = desc if desc else f"Stop {bus_stop_code}"
            if service_no:
                label += f" · {service_no}"
            favourites[key] = label
            await query.answer(f"⭐ Registered: {label}", show_alert=True)


# ---------------------------------------------------------------------------
# User tracking & block gate  (group -1 — runs before all other handlers)
# ---------------------------------------------------------------------------

async def track_and_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if await asyncio.to_thread(bus_stops.is_blocked, user.id):
        raise ApplicationHandlerStop
    await asyncio.to_thread(
        bus_stops.record_user,
        user.id,
        user.username,
        (user.full_name or "").strip(),
    )


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    await _send_admin_home(update.message.reply_text)


async def _send_admin_home(send_fn) -> None:
    total, blocked_list = await asyncio.gather(
        asyncio.to_thread(bus_stops.get_user_count),
        asyncio.to_thread(bus_stops.get_blocked_users),
    )
    blocked = len(blocked_list)
    text = (
        f"🔧 <b>Admin Panel</b>\n\n"
        f"👥 Users: <b>{total}</b>  ·  🚫 Blocked: <b>{blocked}</b>"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("👥 Users", callback_data="admin_users:0"),
        InlineKeyboardButton("🚫 Blocked", callback_data="admin_blocked"),
    ]])
    await send_fn(text, parse_mode="HTML", reply_markup=keyboard)


async def _users_page_content(page: int) -> tuple[str, InlineKeyboardMarkup]:
    users, total = await asyncio.gather(
        asyncio.to_thread(bus_stops.get_users, page),
        asyncio.to_thread(bus_stops.get_user_count),
    )
    per_page = bus_stops.ADMIN_PAGE_SIZE
    total_pages = max(1, (total + per_page - 1) // per_page)

    lines = [f"👥 <b>Users</b>  ·  {total} total\n"]
    for u in users:
        label = _h(_user_label(u))
        name = _h(u["full_name"] or "")
        since = _relative_time(u["last_seen"])
        blocked_tag = "  🚫" if u["blocked"] else ""
        lines.append(f"<b>{label}</b>{blocked_tag}  {name}  ·  {u['msg_count']} msgs  ·  {since}")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("←", callback_data=f"admin_users:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="noop"))
    if (page + 1) < total_pages:
        nav.append(InlineKeyboardButton("→", callback_data=f"admin_users:{page + 1}"))

    block_btns = [
        InlineKeyboardButton(
            f"🚫 {_user_label(u)[:12]}",
            callback_data=f"admin_blk_ask:{u['id']}:{page}",
        )
        for u in users if not u["blocked"]
    ]

    rows = [nav]
    for i in range(0, len(block_btns), 3):
        rows.append(block_btns[i : i + 3])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_home")])

    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _blocked_page_content() -> tuple[str, InlineKeyboardMarkup]:
    blocked = await asyncio.to_thread(bus_stops.get_blocked_users)
    if not blocked:
        text = "🚫 <b>Blocked Users</b>\n\n<i>Nobody blocked.</i>"
    else:
        lines = [f"🚫 <b>Blocked Users</b>  ·  {len(blocked)} total\n"]
        for u in blocked:
            label = _h(_user_label(u))
            since = _relative_time(u["blocked_at"])
            lines.append(f"<b>{label}</b>  ·  blocked {since}")
        text = "\n".join(lines)

    unblock_btns = [
        InlineKeyboardButton(
            f"✅ {_user_label(u)[:12]}",
            callback_data=f"admin_unblock:{u['id']}",
        )
        for u in blocked
    ]
    rows = []
    for i in range(0, len(unblock_btns), 3):
        rows.append(unblock_btns[i : i + 3])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_home")])
    return text, InlineKeyboardMarkup(rows)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    await query.answer()
    data = query.data

    if data == "noop":
        return

    if data == "admin_home":
        await _send_admin_home(query.edit_message_text)
        return

    if data.startswith("admin_users:"):
        page = int(data.split(":")[1])
        text, kb = await _users_page_content(page)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data == "admin_blocked":
        text, kb = await _blocked_page_content()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("admin_blk_ask:"):
        _, uid_str, page_str = data.split(":")
        uid = int(uid_str)
        u = await asyncio.to_thread(bus_stops.get_user, uid)
        label = _h(_user_label(u) if u else f"#{uid}")
        full = _h((u or {}).get("full_name") or "")
        msgs = (u or {}).get("msg_count", "?")
        await query.edit_message_text(
            f"🚫 Block <b>{label}</b>?\n<i>{full}  ·  {msgs} messages</i>\n\n"
            "They will be silently ignored from this point on.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Block", callback_data=f"admin_blk_do:{uid}:{page_str}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"admin_users:{page_str}"),
            ]]),
        )
        return

    if data.startswith("admin_blk_do:"):
        _, uid_str, page_str = data.split(":")
        uid = int(uid_str)
        u = await asyncio.to_thread(bus_stops.get_user, uid)
        label = _user_label(u) if u else f"#{uid}"
        await asyncio.to_thread(bus_stops.block_user, uid)
        await query.answer(f"🚫 {label} blocked", show_alert=True)
        text, kb = await _users_page_content(int(page_str))
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("admin_unblock:"):
        uid = int(data.split(":")[1])
        u = await asyncio.to_thread(bus_stops.get_user, uid)
        label = _user_label(u) if u else f"#{uid}"
        await asyncio.to_thread(bus_stops.unblock_user, uid)
        await query.answer(f"✅ {label} unblocked", show_alert=True)
        text, kb = await _blocked_page_content()
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    global _http_client
    _http_client = httpx.AsyncClient(timeout=10)
    await bus_stops.ensure_loaded(LTA_API_KEY)

    public_commands = [
        BotCommand("stop", "Check arrivals · /stop 83139"),
        BotCommand("favourites", "Your saved stops"),
        BotCommand("dashboard", "Live view of all saved stops"),
        BotCommand("about", "Full guide & data info"),
        BotCommand("help", "Quick reference"),
        BotCommand("start", "Welcome message"),
    ]
    await app.bot.set_my_commands(public_commands)

    if ADMIN_USER_IDS:
        admin_commands = public_commands + [
            BotCommand("admin", "Admin panel"),
            BotCommand("refreshstops", "Sync bus stop database"),
        ]
        for admin_id in ADMIN_USER_IDS:
            await app.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )


async def post_shutdown(app: Application) -> None:
    if _http_client:
        await _http_client.aclose()


def main() -> None:
    persistence = PicklePersistence(filepath="data/bot_data.pickle")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Group -1: runs before everything else — tracks users, blocks bad actors
    app.add_handler(MessageHandler(filters.ALL, track_and_block), group=-1)
    app.add_handler(CallbackQueryHandler(track_and_block), group=-1)

    # Group 0: normal handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("favourites", favourites_command))
    app.add_handler(CommandHandler("dashboard", dashboard_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("refreshstops", refreshstops_command))
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern=r"^(admin_|noop)"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

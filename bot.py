"""
Zomato & Swiggy Deal Alert Bot (GitHub Actions & Cloud-Ready)

Supports:
  - One-shot mode (`python bot.py --once`) for GitHub Actions cron workflows.
  - Continuous mode (`python bot.py --daemon`) for VPS / PaaS / local hosting.
"""

import argparse
import html
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_ZOMATO_PAGES = [
    "/bangalore/delivery-in-btm",
    "/bangalore/delivery-in-koramangala",
    "/bangalore/delivery-in-hsr",
    "/bangalore/delivery-in-jp-nagar",
]

DEAL_TTL_SECONDS = 12 * 3600  # Re-alert repeating daily deals after 12 hours


def load_config() -> Dict[str, Any]:
    """Load runtime configuration purely from environment variables."""
    pages_raw = os.environ.get("ZOMATO_PAGES", "").strip()
    zomato_pages = (
        [p.strip() for p in pages_raw.split(",") if p.strip()]
        if pages_raw
        else list(DEFAULT_ZOMATO_PAGES)
    )

    def _json_env(name: str) -> Dict[str, Any]:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    proxy_url = os.environ.get("PROXY_URL", "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    return {
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "location_name": os.environ.get("TARGET_LOCATION", "BTM 2nd Stage, Bangalore").strip(),
        "lat": float(os.environ.get("LAT", "12.9166")),
        "lon": float(os.environ.get("LON", "77.6101")),
        "min_percent_off": int(os.environ.get("MIN_PERCENT_OFF", "40")),
        "min_flat_off": int(os.environ.get("MIN_FLAT_OFF", "100")),
        "state_file": os.environ.get("STATE_FILE", "state/bot_state.json").strip(),
        "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL_SECONDS", "300")),
        "zomato_pages": zomato_pages,
        "swiggy_cookies": _json_env("SWIGGY_COOKIES_JSON"),
        "zomato_cookies": _json_env("ZOMATO_COOKIES_JSON"),
        "zomato_mqtt_conf": _json_env("ZOMATO_MQTT_CONF_JSON"),
        "proxies": proxies,
    }


def evaluate_flash_deal(
    discount_text: str,
    min_percent_off: int = 40,
    min_flat_off: int = 100,
) -> Tuple[bool, int]:
    """Return (is_flash_deal, ranking_score) for a discount label."""
    if not discount_text:
        return False, 0

    upper = discount_text.upper()
    pct_matches = [int(m) for m in re.findall(r"(\d{1,3})\s*%", upper)]
    flat_matches = [
        int(m) for m in re.findall(r"(?:₹|RS\.?|INR)\s*(\d{2,4})", upper)
    ]

    max_pct = max(pct_matches, default=0)
    max_flat = max(flat_matches, default=0)
    has_keyword = any(k in upper for k in ("BOGO", "BUY 1", "STEAL"))

    is_flash = (max_pct >= min_percent_off) or (max_flat >= min_flat_off) or has_keyword
    score = max(max_pct * 3, max_flat) + (200 if has_keyword else 0)
    return is_flash, score


def parse_zomato_page_payload(
    data: Dict[str, Any],
    min_percent_off: int = 40,
    min_flat_off: int = 100,
) -> List[Dict[str, Any]]:
    """Extract restaurant deals from a Zomato `/webroutes/getPage` JSON payload."""
    deals: List[Dict[str, Any]] = []
    sections = data.get("page_data", {}).get("sections", {})
    results = sections.get("SECTION_SEARCH_RESULT", [])

    for item in results:
        info = item.get("info")
        if not isinstance(info, dict) or not info.get("name"):
            continue

        rest_id = str(info.get("resId") or info.get("name"))
        name = str(info.get("name")).strip()
        locality = (info.get("locality") or {}).get("name") or ""
        rating = (info.get("rating") or {}).get("aggregate_rating") or ""
        cost = (info.get("cft") or {}).get("text") or ""
        sla = (item.get("order") or {}).get("deliveryTime") or ""

        cuisines_raw = info.get("cuisine") or []
        cuisines = ", ".join(
            str(c.get("name")) for c in cuisines_raw[:3] if isinstance(c, dict) and c.get("name")
        )

        click_url = (item.get("cardAction") or {}).get("clickUrl") or ""
        full_url = (
            f"https://www.zomato.com{click_url}"
            if click_url.startswith("/")
            else (click_url or "https://www.zomato.com/bangalore/delivery-in-btm")
        )

        offer_parts: List[str] = []
        for b in item.get("bulkOffers") or []:
            if isinstance(b, dict) and b.get("text"):
                offer_parts.append(str(b["text"]).strip())

        gold = item.get("gold")
        if isinstance(gold, dict) and gold.get("offerValue"):
            prefix = f"{gold.get('text', '')} ".strip()
            offer_parts.append(f"{prefix} {gold['offerValue']}".strip())

        discount = " | ".join(dict.fromkeys(offer_parts))
        is_flash, score = evaluate_flash_deal(discount, min_percent_off, min_flat_off)

        deals.append(
            {
                "source": "zomato",
                "id": rest_id,
                "name": name,
                "rating": str(rating),
                "area": locality or "Bangalore",
                "sla": sla,
                "discount": discount,
                "cost": cost,
                "cuisines": cuisines,
                "url": full_url,
                "is_flash": is_flash,
                "score": score,
            }
        )
    return deals


def parse_swiggy_listing_payload(
    data: Dict[str, Any],
    min_percent_off: int = 40,
    min_flat_off: int = 100,
) -> List[Dict[str, Any]]:
    """Extract deals from Swiggy `/dapi/restaurants/list/v5` JSON if discounts are present."""
    deals: List[Dict[str, Any]] = []
    cards = data.get("data", {}).get("cards", [])
    for c in cards:
        grid = (
            c.get("card", {})
            .get("card", {})
            .get("gridElements", {})
            .get("infoWithStyle", {})
        )
        for r in grid.get("restaurants", []):
            info = r.get("info") or {}
            name = info.get("name")
            if not name:
                continue
            v3 = info.get("aggregatedDiscountInfoV3") or {}
            v2 = info.get("aggregatedDiscountInfoV2") or {}
            header = v3.get("header") or v2.get("header") or ""
            sub = v3.get("subHeader") or v2.get("subHeader") or ""
            discount = f"{header} {sub}".strip()
            if not discount:
                continue

            is_flash, score = evaluate_flash_deal(discount, min_percent_off, min_flat_off)
            rest_id = str(info.get("id") or name)
            deals.append(
                {
                    "source": "swiggy",
                    "id": rest_id,
                    "name": name,
                    "rating": str(info.get("avgRating") or ""),
                    "area": info.get("areaName") or "BTM",
                    "sla": (info.get("sla") or {}).get("slaString") or "",
                    "discount": discount,
                    "cost": info.get("costForTwo") or "",
                    "cuisines": ", ".join((info.get("cuisines") or [])[:3]),
                    "url": "https://www.swiggy.com/",
                    "is_flash": is_flash,
                    "score": score,
                }
            )
    return deals


def parse_swiggy_offers_html(
    html_text: str,
    min_percent_off: int = 40,
    min_flat_off: int = 100,
) -> List[Dict[str, Any]]:
    """Extract offers from Swiggy `/offers-near-me` `__NEXT_DATA__` payload."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S)
    if not m:
        return []
    try:
        nd = json.loads(m.group(1))
    except Exception:
        return []

    deals: List[Dict[str, Any]] = []
    cards = (
        nd.get("props", {})
        .get("pageProps", {})
        .get("widgetResponse", {})
        .get("success", {})
        .get("cards", [])
    )
    for c in cards:
        rests = (
            c.get("card", {})
            .get("card", {})
            .get("gridElements", {})
            .get("infoWithStyle", {})
            .get("restaurants", [])
        )
        for r in rests:
            info = r.get("info") or {}
            name = info.get("name")
            if not name:
                continue

            offer_texts: List[str] = []
            cust_offer = (info.get("offerInfoV2") or {}).get("customerOffer") or {}
            if cust_offer.get("header"):
                offer_texts.append(str(cust_offer["header"]).strip())

            for o in ((info.get("offerInfoV2") or {}).get("otherOffers") or {}).get("offers") or []:
                if isinstance(o, dict) and o.get("header"):
                    offer_texts.append(str(o["header"]).strip())

            loyalty_detail = (
                (info.get("customerOffer") or {}).get("loyaltyInfo") or {}
            ).get("offerDetail") or {}
            if loyalty_detail.get("title"):
                sub = loyalty_detail.get("subtitle") or ""
                offer_texts.append(f"{loyalty_detail['title']} {sub}".strip())

            for vo in (info.get("vendorOffer") or {}).get("infos") or []:
                if isinstance(vo, dict) and vo.get("header"):
                    offer_texts.append(str(vo["header"]).strip())

            discount = " | ".join(dict.fromkeys(offer_texts))
            if not discount:
                continue

            is_flash, score = evaluate_flash_deal(discount, min_percent_off, min_flat_off)
            rating_val = info.get("rating")
            if isinstance(rating_val, dict):
                rating_str = str(rating_val.get("avgRating") or "")
            else:
                rating_str = str(rating_val or "")

            deals.append(
                {
                    "source": "swiggy",
                    "id": str(info.get("id") or name),
                    "name": name,
                    "rating": rating_str,
                    "area": info.get("locality") or info.get("displayAreaName") or "Bangalore",
                    "sla": "Dineout / Offer",
                    "discount": discount,
                    "cost": info.get("costForTwo") or "",
                    "cuisines": ", ".join((info.get("cuisines") or [])[:3]),
                    "url": "https://www.swiggy.com/offers-near-me",
                    "is_flash": is_flash,
                    "score": score,
                }
            )
    return deals


def fetch_zomato_deals(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch live delivery offers from Zomato across configured area pages."""
    headers = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://www.zomato.com/",
        "Accept": "application/json",
    }
    if cfg.get("zomato_cookies"):
        headers["Cookie"] = "; ".join(
            f"{k}={v}" for k, v in cfg["zomato_cookies"].items() if v
        )

    by_id: Dict[str, Dict[str, Any]] = {}
    for page_url in cfg["zomato_pages"]:
        url = (
            "https://www.zomato.com/webroutes/getPage?page_url="
            + urllib.parse.quote(page_url, safe="/?=&")
        )
        try:
            resp = requests.get(
                url, headers=headers, proxies=cfg.get("proxies"), timeout=15
            )
            if resp.status_code != 200:
                print(f"[Zomato] {page_url} returned HTTP {resp.status_code}")
                continue
            parsed = parse_zomato_page_payload(
                resp.json(),
                min_percent_off=cfg["min_percent_off"],
                min_flat_off=cfg["min_flat_off"],
            )
            for deal in parsed:
                key = deal["id"]
                if key not in by_id or deal["score"] > by_id[key]["score"]:
                    by_id[key] = deal
        except Exception as exc:
            print(f"[Zomato] Error fetching {page_url}: {exc}")

    return list(by_id.values())


def fetch_swiggy_deals(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch live Swiggy offers from listing API and offers-near-me page."""
    by_id: Dict[str, Dict[str, Any]] = {}

    headers = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://www.swiggy.com/",
        "Accept": "application/json",
    }
    if cfg.get("swiggy_cookies"):
        headers["Cookie"] = "; ".join(
            f"{k}={v}" for k, v in cfg["swiggy_cookies"].items() if v
        )

    try:
        resp = requests.get(
            "https://www.swiggy.com/dapi/restaurants/list/v5",
            headers=headers,
            params={
                "lat": str(cfg["lat"]),
                "lng": str(cfg["lon"]),
                "is-seo-homepage-enabled": "true",
                "page_type": "DESKTOP_WEB_LISTING",
            },
            proxies=cfg.get("proxies"),
            timeout=15,
        )
        if resp.status_code == 200:
            for deal in parse_swiggy_listing_payload(
                resp.json(), cfg["min_percent_off"], cfg["min_flat_off"]
            ):
                by_id[deal["id"]] = deal
        else:
            print(f"[Swiggy] listing API returned HTTP {resp.status_code}")
    except Exception as exc:
        print(f"[Swiggy] listing API error: {exc}")

    user_loc = urllib.parse.quote(
        json.dumps(
            {
                "lat": cfg["lat"],
                "lng": cfg["lon"],
                "address": cfg["location_name"],
                "area": "BTM Layout",
            }
        )
    )
    offer_cookie = f"userLocation={user_loc}"
    if headers.get("Cookie"):
        offer_cookie = f"{headers['Cookie']}; {offer_cookie}"

    try:
        resp_offers = requests.get(
            "https://www.swiggy.com/offers-near-me",
            headers={"User-Agent": DEFAULT_UA, "Cookie": offer_cookie},
            proxies=cfg.get("proxies"),
            timeout=15,
        )
        if resp_offers.status_code == 200:
            for deal in parse_swiggy_offers_html(
                resp_offers.text, cfg["min_percent_off"], cfg["min_flat_off"]
            ):
                if deal["id"] not in by_id or deal["score"] > by_id[deal["id"]]["score"]:
                    by_id[deal["id"]] = deal
        else:
            print(f"[Swiggy] offers-near-me returned HTTP {resp_offers.status_code}")
    except Exception as exc:
        print(f"[Swiggy] offers-near-me error: {exc}")

    return list(by_id.values())


def load_state(state_file: str) -> Dict[str, Any]:
    """Load persisted bot state from JSON file."""
    default = {
        "seen_deals": {},
        "last_update_id": 0,
        "chat_id": "",
        "last_run_at": "",
        "last_zomato_count": 0,
        "last_swiggy_count": 0,
    }
    if not state_file or not os.path.exists(state_file):
        return default
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            default.update(loaded)
    except Exception as exc:
        print(f"[State] Failed reading {state_file}: {exc}")
    if not isinstance(default.get("seen_deals"), dict):
        default["seen_deals"] = {}
    return default


def prune_seen_deals(seen_deals: Dict[str, float], now_ts: Optional[float] = None) -> Dict[str, float]:
    """Remove seen deal keys older than DEAL_TTL_SECONDS."""
    now = now_ts if now_ts is not None else time.time()
    return {
        k: ts
        for k, ts in seen_deals.items()
        if isinstance(ts, (int, float)) and (now - ts) < DEAL_TTL_SECONDS
    }


def save_state(state_file: str, state: Dict[str, Any]) -> None:
    """Persist bot state cleanly to JSON file."""
    if not state_file:
        return
    parent = os.path.dirname(os.path.abspath(state_file))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
) -> bool:
    """Send a Telegram message, splitting cleanly if close to Telegram's 4096-char limit."""
    if not bot_token or not chat_id:
        print("[Telegram] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    chunks: List[str] = []
    remaining = text
    while len(remaining) > 3900:
        split_at = remaining.rfind("\n\n", 0, 3900)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, 3900)
        if split_at == -1:
            split_at = 3900
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    all_ok = True
    for chunk in chunks:
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[Telegram] Error HTTP {resp.status_code}: {resp.text[:200]}")
                all_ok = False
        except Exception as exc:
            print(f"[Telegram] Send exception: {exc}")
            all_ok = False
    return all_ok


def format_deal_line(deal: Dict[str, Any], idx: Optional[int] = None) -> str:
    """Format one restaurant deal as HTML for Telegram."""
    prefix = f"<b>{idx}. " if idx is not None else "<b>"
    source_tag = "🍕 Zomato" if deal["source"] == "zomato" else "⚡ Swiggy"
    name = html.escape(deal["name"])
    area = html.escape(deal["area"])
    disc = html.escape(deal["discount"])
    url = html.escape(deal["url"], quote=True)

    meta_parts = []
    if deal.get("rating"):
        meta_parts.append(f"⭐ {html.escape(deal['rating'])}")
    if deal.get("sla"):
        meta_parts.append(f"⏱️ {html.escape(deal['sla'])}")
    if deal.get("cost"):
        meta_parts.append(f"💰 {html.escape(deal['cost'])}")
    meta_line = " | ".join(meta_parts)

    lines = [f"{prefix}<a href='{url}'>{name}</a></b> ({area}) — <i>{source_tag}</i>"]
    lines.append(f"   🏷️ <b>{disc}</b>")
    if meta_line:
        lines.append(f"   {meta_line}")
    if deal.get("cuisines"):
        lines.append(f"   🍽️ {html.escape(deal['cuisines'])}")
    return "\n".join(lines)


def format_new_flash_digest(deals: List[Dict[str, Any]], location_name: str) -> str:
    """Build a single readable Telegram alert message for newly discovered flash deals."""
    top = sorted(deals, key=lambda d: (-d["score"], d["name"]))[:10]
    header = (
        f"🔥 <b>NEW FOOD DEALS IN {html.escape(location_name.upper())}</b> 🔥\n"
        f"<i>{len(deals)} new high-value offer(s) detected:</i>\n\n"
    )
    body = "\n\n".join(format_deal_line(d, idx=i) for i, d in enumerate(top, 1))
    footer = ""
    if len(deals) > len(top):
        footer = f"\n\n<i>+{len(deals) - len(top)} more deals available via /deals</i>"
    return header + body + footer


def handle_command(
    text: str,
    first_name: str,
    chat_id: str,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    cached_deals: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Build a reply for a Telegram command and optionally reuse/fetch live deals."""
    cmd = text.split()[0].lower() if text else ""
    deals = cached_deals

    if cmd.startswith("/start"):
        return (
            f"👋 <b>Welcome {html.escape(first_name)}!</b>\n\n"
            f"🤖 <b>Zomato & Swiggy Alert Bot</b> is active for <b>{html.escape(cfg['location_name'])}</b>.\n\n"
            f"🛠️ <b>Commands:</b>\n"
            f"/deals — Live top Zomato & Swiggy offers nearby\n"
            f"/zomato — Top Zomato delivery offers\n"
            f"/swiggy — Top Swiggy offers\n"
            f"/status — Last scan stats & active filters\n"
            f"/test_rescue — Preview Zomato Food Rescue alert\n"
            f"/help — How alerts work"
        ), (deals or [])

    if cmd.startswith("/status"):
        mqtt_status = (
            "🟢 Configured"
            if cfg.get("zomato_mqtt_conf")
            else "🟡 Standby (set ZOMATO_MQTT_CONF_JSON for live MQTT)"
        )
        return (
            f"📊 <b>BOT STATUS</b>\n\n"
            f"📍 <b>Location:</b> {html.escape(cfg['location_name'])}\n"
            f"🎯 <b>Alert Threshold:</b> {cfg['min_percent_off']}%+ OFF or ₹{cfg['min_flat_off']}+ OFF\n"
            f"🍕 <b>Last Zomato Scan:</b> {state.get('last_zomato_count', 0)} restaurants\n"
            f"⚡ <b>Last Swiggy Scan:</b> {state.get('last_swiggy_count', 0)} restaurants\n"
            f"📡 <b>Zomato Food Rescue MQTT:</b> {mqtt_status}\n"
            f"🗂️ <b>Tracked Alerts (12h window):</b> {len(state.get('seen_deals', {}))}\n"
            f"🕒 <b>Last Run (UTC):</b> {html.escape(state.get('last_run_at') or 'Never')}"
        ), (deals or [])

    if cmd.startswith("/deals") or cmd.startswith("/zomato") or cmd.startswith("/swiggy"):
        if deals is None:
            deals = fetch_zomato_deals(cfg) + fetch_swiggy_deals(cfg)
        if cmd.startswith("/zomato"):
            filtered = [d for d in deals if d["source"] == "zomato"]
            title = "ZOMATO DELIVERY DEALS"
        elif cmd.startswith("/swiggy"):
            filtered = [d for d in deals if d["source"] == "swiggy"]
            title = "SWIGGY OFFERS NEARBY"
        else:
            filtered = list(deals)
            title = "TOP ZOMATO & SWIGGY DEALS"

        ranked = sorted(filtered, key=lambda d: (-int(d["is_flash"]), -d["score"], d["name"]))[:10]
        if not ranked:
            return "No live deals were returned right now. Try again shortly.", deals
        lines = "\n\n".join(format_deal_line(d, idx=i) for i, d in enumerate(ranked, 1))
        return f"🔥 <b>{title} ({html.escape(cfg['location_name'])}):</b>\n\n{lines}", deals

    if cmd.startswith("/test_rescue"):
        return (
            f"🧪 <b>[TEST] ZOMATO FOOD RESCUE ALERT</b>\n\n"
            f"🚨 <b>Order Cancelled Nearby!</b>\n"
            f"📍 <b>Area:</b> {html.escape(cfg['location_name'])}\n"
            f"🏪 <b>Restaurant:</b> <b>Meghana Foods - BTM</b>\n"
            f"🍲 <b>Items:</b> 1x Special Chicken Biryani + 1x Paneer 65\n"
            f"💰 <b>Rescue Price:</b> <b>₹249</b> <i>(was ₹590)</i> — <b>58% OFF!</b>"
        ), (deals or [])

    if cmd.startswith("/test_swiggy"):
        return (
            f"🧪 <b>[TEST] SWIGGY FLASH SALE ALERT</b>\n\n"
            f"🏪 <b>Restaurant:</b> <b>Truffles (Koramangala / BTM)</b>\n"
            f"🏷️ <b>Offer:</b> <b>FLAT 50% OFF (Up to ₹150)</b>"
        ), (deals or [])

    if cmd.startswith("/help"):
        return (
            f"ℹ️ <b>HOW THIS BOT WORKS</b>\n\n"
            f"• Runs on GitHub Actions every ~5 minutes (even when your PC is off).\n"
            f"• Alerts automatically whenever a restaurant posts <b>{cfg['min_percent_off']}%+ OFF</b>, "
            f"<b>₹{cfg['min_flat_off']}+ OFF</b>, or <b>BOGO/Steal</b> offers.\n"
            f"• Send /deals anytime to get top offers on the next scheduled check (or trigger the workflow manually on GitHub)."
        ), (deals or [])

    return 'Use /deals for live offers, /zomato, /swiggy, or /status for bot health.', (deals or [])


def process_telegram_updates(
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    timeout_seconds: int = 0,
    cached_deals: Optional[List[Dict[str, Any]]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch and reply to pending Telegram bot updates."""
    token = cfg["bot_token"]
    if not token:
        return cached_deals

    offset = int(state.get("last_update_id", 0)) + 1
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        resp = requests.get(
            url,
            params={"offset": offset, "timeout": timeout_seconds},
            timeout=max(10, timeout_seconds + 5),
        )
        if resp.status_code != 200:
            print(f"[Telegram] getUpdates HTTP {resp.status_code}: {resp.text[:200]}")
            return cached_deals
        payload = resp.json()
    except Exception as exc:
        print(f"[Telegram] getUpdates error: {exc}")
        return cached_deals

    if not payload.get("ok"):
        return cached_deals

    for update in payload.get("result", []):
        state["last_update_id"] = max(int(state.get("last_update_id", 0)), int(update["update_id"]))
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue
        chat = msg.get("chat") or {}
        cid = str(chat.get("id") or "")
        text = (msg.get("text") or "").strip()
        first_name = chat.get("first_name") or "Friend"
        if cid:
            state["chat_id"] = cid
        if not text:
            continue

        print(f"[Telegram] Command from {first_name} ({cid}): {text}")
        reply, cached_deals = handle_command(
            text, first_name, cid, cfg, state, cached_deals=cached_deals
        )
        if reply:
            send_telegram(token, cid, reply)

    return cached_deals


def run_once(cfg: Dict[str, Any]) -> int:
    """Execute one complete scan + Telegram update pass (ideal for GitHub Actions cron)."""
    state = load_state(cfg["state_file"])
    state["seen_deals"] = prune_seen_deals(state.get("seen_deals", {}))

    zomato_deals = fetch_zomato_deals(cfg)
    swiggy_deals = fetch_swiggy_deals(cfg)
    all_deals = zomato_deals + swiggy_deals

    state["last_zomato_count"] = len(zomato_deals)
    state["last_swiggy_count"] = len(swiggy_deals)
    state["last_run_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(
        f"[Scan] Zomato={len(zomato_deals)} deals, Swiggy={len(swiggy_deals)} deals, "
        f"Flash={sum(1 for d in all_deals if d['is_flash'])}"
    )

    # Reply to any pending Telegram commands first (reusing the freshly fetched deals)
    process_telegram_updates(cfg, state, timeout_seconds=0, cached_deals=all_deals)

    chat_id = cfg["chat_id"] or state.get("chat_id", "")
    now_ts = time.time()
    new_flash: List[Dict[str, Any]] = []
    for deal in all_deals:
        if not deal["is_flash"]:
            continue
        deal_key = f"{deal['source']}:{deal['id']}:{deal['discount']}"
        if deal_key not in state["seen_deals"]:
            state["seen_deals"][deal_key] = now_ts
            new_flash.append(deal)

    if new_flash and chat_id and cfg["bot_token"]:
        digest = format_new_flash_digest(new_flash, cfg["location_name"])
        send_telegram(cfg["bot_token"], chat_id, digest)
        print(f"[Alert] Sent {len(new_flash)} new flash deal(s) to chat {chat_id}.")
    else:
        print(f"[Alert] 0 new flash deals to send (tracked={len(state['seen_deals'])}).")

    save_state(cfg["state_file"], state)
    return len(new_flash)


def _start_optional_mqtt(cfg: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Start Zomato Food Rescue MQTT listener if credentials JSON is configured."""
    conf = cfg.get("zomato_mqtt_conf") or {}
    if not conf or mqtt is None:
        return
    user = conf.get("username")
    pw = conf.get("password")
    topics = conf.get("topics") or []
    if not (user and pw and topics):
        return

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"zomato_rescue_{int(time.time())}",
        transport="tcp",
    )

    def on_connect(c, _u, _f, rc, _p=None):
        if rc == 0:
            for t in topics:
                c.subscribe(t, qos=1)
            print(f"[MQTT] Connected and subscribed to {len(topics)} topic(s).")

    def on_message(_c, _u, msg):
        chat_id = cfg["chat_id"] or state.get("chat_id", "")
        if not chat_id:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            rest = html.escape(str(payload.get("restaurant_name", "Nearby Restaurant")))
            items = payload.get("items", "Cancelled Order Basket")
            items_str = html.escape(", ".join(map(str, items)) if isinstance(items, list) else str(items))
            price = html.escape(str(payload.get("discounted_price", payload.get("price", ""))))
            orig = html.escape(str(payload.get("original_price", payload.get("mrp", ""))))
            text = (
                f"🚨 <b>ZOMATO FOOD RESCUE ALERT!</b> 🚨\n\n"
                f"🏪 <b>{rest}</b>\n"
                f"🍱 {items_str}\n"
                f"💰 <b>₹{price}</b> (was ₹{orig})\n"
                f"👉 Open Zomato > Food Rescue immediately!"
            )
            send_telegram(cfg["bot_token"], chat_id, text)
        except Exception as exc:
            print(f"[MQTT] Parse error: {exc}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.username_pw_set(user, pw)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.connect("consumermqtt.zomato.com", 443, keepalive=900)
    threading.Thread(target=client.loop_forever, daemon=True).start()


def run_daemon(cfg: Dict[str, Any]) -> None:
    """Continuous polling mode for 24/7 cloud VMs / containers."""
    print(f"[Daemon] Starting continuous monitor for {cfg['location_name']}...")
    state = load_state(cfg["state_file"])
    _start_optional_mqtt(cfg, state)

    next_scan = 0.0
    while True:
        now = time.time()
        if now >= next_scan:
            run_once(cfg)
            state = load_state(cfg["state_file"])
            next_scan = now + cfg["poll_interval_seconds"]
        process_telegram_updates(cfg, state, timeout_seconds=15)
        save_state(cfg["state_file"], state)
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Zomato & Swiggy Deal Alert Bot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="Run a single scan + update cycle and exit")
    group.add_argument("--daemon", action="store_true", help="Run continuously in a polling loop")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg["bot_token"]:
        print("[Error] TELEGRAM_BOT_TOKEN environment variable is required.", file=sys.stderr)
        sys.exit(1)

    if args.daemon:
        run_daemon(cfg)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()

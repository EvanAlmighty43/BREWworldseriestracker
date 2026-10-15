"""World Series ticket price tracker (standing-room-only filtered).

Checks SeatGeek (official API, optional) plus StubHub, Vivid Seats and
Gametime (public event pages loaded in a headless browser). For the browser
sites it reads the individual listings the page loads, drops any that look
like standing room only, and records the cheapest remaining price. Results go
to prices.csv, with Discord alerts on low prices or sharp drops.
"""
import csv
import datetime as dt
import json
import os
import re
from pathlib import Path

import requests

CONFIG = json.loads(Path("config.json").read_text())
HISTORY = Path("prices.csv")
FIELDS = ["timestamp", "game", "site", "lowest"]
SITES = ["stubhub", "vividseats", "gametime"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Listings whose own data mentions any of these are treated as standing room.
SRO_RE = re.compile(r"standing\s*room|\bSRO\b|\bstanding\b", re.I)
PRICE_KEYS = {"price", "listingprice", "currentprice", "rawprice",
              "priceperticket", "displayprice"}
MIN_LISTINGS = 3  # need at least this many listings to trust the result


# ---------- listing-level extraction ----------
def _strings(node, depth=0):
    """Yield every string inside a JSON node (a few levels deep)."""
    if depth > 4:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v, depth + 1)


def _num(v):
    if isinstance(v, dict):
        v = v.get("amount") or v.get("value") or v.get("raw")
    if isinstance(v, str):
        v = re.sub(r"[^0-9.]", "", v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect_listings(node, prices, skipped):
    """Find listing-like dicts (they carry a price) and sort them into
    normal listings (prices) and standing-room ones (skipped count)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in PRICE_KEYS:
                p = _num(v)
                if p and 1 <= p <= 100000:
                    if any(SRO_RE.search(s) for s in _strings(node)):
                        skipped.append(p)
                    else:
                        prices.append(p)
                break
        for v in node.values():
            collect_listings(v, prices, skipped)
    elif isinstance(node, list):
        for v in node:
            collect_listings(v, prices, skipped)


# ---------- aggregate fallback (cannot exclude standing room) ----------
def _walk(node):
    if isinstance(node, dict):
        if "lowPrice" in node:
            yield node["lowPrice"]
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def price_from_html(html):
    prices = []
    pattern = r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>'
    for m in re.finditer(pattern, html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for v in _walk(data):
            try:
                prices.append(float(v))
            except (TypeError, ValueError):
                pass
    return min(prices) if prices else None


# ---------- data sources ----------
def seatgeek_price(event_id):
    """SeatGeek's API only gives an all-listings minimum, which can include
    standing room. Leave seatgeek_id as null in config.json to skip it."""
    client_id = os.environ.get("SEATGEEK_CLIENT_ID")
    if not (event_id and client_id):
        return None
    r = requests.get(
        f"https://api.seatgeek.com/2/events/{event_id}",
        params={"client_id": client_id},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["stats"].get("lowest_price")


def browser_prices(urls):
    results = {}
    if not urls:
        return results
    from playwright.sync_api import sync_playwright

    allow_fallback = CONFIG.get("fallback_unfiltered", False)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        for key, url in urls.items():
            page = ctx.new_page()
            json_responses = []
            page.on("response", lambda r, bucket=json_responses:
                    bucket.append(r)
                    if "json" in (r.headers.get("content-type") or "") else None)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)

                prices, skipped = [], []
                for resp in json_responses:
                    try:
                        collect_listings(resp.json(), prices, skipped)
                    except Exception:  # noqa: BLE001
                        pass

                if len(prices) >= MIN_LISTINGS:
                    results[key] = min(prices)
                    print(f"{key}: {len(prices)} listings, "
                          f"{len(skipped)} standing-room skipped, "
                          f"lowest ${results[key]:,.0f}")
                elif allow_fallback:
                    results[key] = price_from_html(page.content())
                    print(f"WARN {key}: could not read individual listings; "
                          f"used page-wide price (may include standing

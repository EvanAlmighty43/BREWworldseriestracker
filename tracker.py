"""World Series ticket price tracker (standing room filtered)."""
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

# Listings mentioning any of these are treated as standing room.
SRO_RE = re.compile(r"standing\s*room|\bSRO\b|\bstanding\b", re.I)
PRICE_KEYS = {
    "price", "listingprice", "currentprice",
    "rawprice", "priceperticket", "displayprice",
}
MIN_LISTINGS = 3


def _strings(node, depth=0):
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
    """Sort listing-like dicts into normal and standing-room lists."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in PRICE_KEYS:
                p = _num(v)
                if p and 1 <= p <= 100000:
                    texts = list(_strings(node))
                    if any(SRO_RE.search(s) for s in texts):
                        skipped.append(p)
                    else:
                        prices.append(p)
                break
        for v in node.values():
            collect_listings(v, prices, skipped)
    elif isinstance(node, list):
        for v in node:
            collect_listings(v, prices, skipped)


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
    """Page-wide fallback price. Cannot exclude standing room."""
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


def seatgeek_price(event_id):
    """Includes standing room. Leave seatgeek_id null to skip it."""
    client_id = os.environ.get("SEATGEEK_CLIENT_ID")
    if not (event_id and

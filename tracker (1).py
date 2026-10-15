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
    if not (event_id and client_id):
        return None
    r = requests.get(
        f"https://api.seatgeek.com/2/events/{event_id}",
        params={"client_id": client_id},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["stats"].get("lowest_price")


def make_handler(bucket):
    def handler(resp):
        ctype = resp.headers.get("content-type") or ""
        if "json" in ctype:
            bucket.append(resp)
    return handler


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
            page.on("response", make_handler(json_responses))
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)

                prices, skipped = [], []
                for resp in json_responses:
                    try:
                        collect_listings(resp.json(), prices, skipped)
                    except Exception:
                        pass

                if len(prices) >= MIN_LISTINGS:
                    low = min(prices)
                    results[key] = low
                    msg = f"{key}: {len(prices)} listings, "
                    msg += f"{len(skipped)} standing room skipped, "
                    msg += f"lowest ${low:,.0f}"
                    print(msg)
                elif allow_fallback:
                    results[key] = price_from_html(page.content())
                    print(f"WARN {key}: used page-wide price")
                else:
                    results[key] = None
                    print(f"WARN {key}: could not read listings")
            except Exception as e:
                print(f"WARN {key}: {e}")
                results[key] = None
            finally:
                page.close()
        browser.close()
    return results


def last_prices():
    last = {}
    if HISTORY.exists():
        with HISTORY.open() as f:
            for row in csv.DictReader(f):
                if row["lowest"]:
                    last[(row["game"], row["site"])] = float(row["lowest"])
    return last


def alert(message):
    print("ALERT:", message)
    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        requests.post(hook, json={"content": message}, timeout=15)


def main():
    previous = last_prices()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    readings = []  # (game, site, price)

    browser_targets = {}
    for game, cfg in CONFIG["events"].items():
        try:
            sg = seatgeek_price(cfg.get("seatgeek_id"))
            readings.append((game, "seatgeek", sg))
        except Exception as e:
            print(f"WARN seatgeek {game}: {e}")
        for site in SITES:
            if cfg.get(site):
                browser_targets[(game, site)] = cfg[site]

    for (game, site), price in browser_prices(browser_targets).items():
        readings.append((game, site, price))

    new_file = not HISTORY.exists()
    with HISTORY.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(FIELDS)
        for game, site, price in readings:
            w.writerow([now, game, site, "" if price is None else price])

    drop_pct = CONFIG.get("drop_pct", 10)
    for game, site, price in readings:
        if price is None:
            continue
        limit = CONFIG["alert_below"].get(game)
        if limit and price < limit:
            alert(f"{game} on {site}: ${price:,.0f} (target ${limit})")
        prev = previous.get((game, site))
        if prev and price <= prev * (1 - drop_pct / 100):
            alert(f"{game} on {site}: dropped ${prev:,.0f} to ${price:,.0f}")


if __name__ == "__main__":
    main()

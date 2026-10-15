"""World Series ticket price tracker.

Checks SeatGeek (official API) plus StubHub, Vivid Seats and Gametime
(public event pages loaded in a headless browser), appends results to
prices.csv, and sends a Discord alert on low prices or sharp drops.
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


# ---------- price extraction ----------
def _walk(node):
    """Yield every 'lowPrice' value found anywhere in parsed JSON."""
    if isinstance(node, dict):
        if "lowPrice" in node:
            yield node["lowPrice"]
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def price_from_html(html):
    """Pull the lowest advertised price from JSON-LD blocks on the page."""
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
    """Load each URL in headless Chromium and extract the lowest price."""
    results = {}
    if not urls:
        return results
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        page = ctx.new_page()
        for key, url in urls.items():
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                results[key] = price_from_html(page.content())
                if results[key] is None:
                    print(f"WARN {key}: page loaded but no price found "
                          "(blocked or layout changed?)")
            except Exception as e:  # noqa: BLE001
                print(f"WARN {key}: {e}")
                results[key] = None
        browser.close()
    return results


# ---------- history + alerts ----------
def last_prices():
    """Most recent stored price per (game, site)."""
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
            readings.append((game, "seatgeek", seatgeek_price(cfg.get("seatgeek_id"))))
        except Exception as e:  # noqa: BLE001
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
            alert(f"{game} on {site}: lowest is ${price:,.0f} (under your ${limit} target)")
        prev = previous.get((game, site))
        if prev and price <= prev * (1 - drop_pct / 100):
            alert(f"{game} on {site}: dropped from ${prev:,.0f} to ${price:,.0f}")


if __name__ == "__main__":
    main()

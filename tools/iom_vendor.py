#!/usr/bin/env python3
"""Normalize an Invent-O-Matic dump and build a safe AutoVend config."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CSV_COLUMNS = [
    "Item Name",
    "Price Lookup Name",
    "Category",
    "Available Quantity",
    "Quantity to Sell",
    "Suggested Price",
    "Source",
    "Already Listed",
    "Current Listed Quantity",
    "Current Vendor Price",
    "Market Low",
    "Market High",
    "Pricing Source",
    "Pricing Notes",
    "Price Checked",
    "NukaTrader Low",
    "NukaTrader High",
    "NukaTrader Recommended",
    "NukaTrader URL",
    "NukaTrader Checked",
    "NukaTrader Source Modified",
    "Level",
    "Legendary Stars",
    "Legendary Effects",
]

PRICE_HISTORY_COLUMNS = [
    "Observed On",
    "Item Name",
    "Price Lookup Name",
    "Category",
    "Source",
    "Market Low",
    "Market High",
    "Recommended Price",
    "Source URL",
    "Source Modified",
]

VALID_TYPES = {
    "WEAPON", "ARMOR", "APPAREL", "FOOD_WATER", "AID", "NOTES",
    "HOLO", "AMMO", "MISC", "MODS", "JUNK",
}

VENDOR_SLOT_LIMIT = 120
ASSIGN_DELAY_MS = 2000
NUKATRADER_SITEMAPS = tuple(
    f"https://nukatrader.com/job_listing-sitemap{suffix}.xml"
    for suffix in ("", "2", "3")
)
NUKATRADER_ROUTES = {
    "APPAREL": "apparel",
    "NOTES": "plans",
    "JUNK": "components",
}
HTTP_USER_AGENT = "fallout76-configs price checker/1.0"
DEFAULT_PRICE_MAX_AGE_DAYS = 30

DATABASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    item_name TEXT NOT NULL,
    lookup_name TEXT NOT NULL,
    category TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT '',
    legendary_stars TEXT NOT NULL DEFAULT '',
    legendary_effects TEXT NOT NULL DEFAULT '',
    UNIQUE(item_name, category, level, legendary_stars, legendary_effects)
);
CREATE TABLE IF NOT EXISTS approved_prices (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    suggested_price INTEGER NOT NULL CHECK(suggested_price BETWEEN 0 AND 40000),
    market_low INTEGER,
    market_high INTEGER,
    pricing_source TEXT NOT NULL DEFAULT '',
    pricing_notes TEXT NOT NULL DEFAULT '',
    price_checked TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    market_low INTEGER,
    market_high INTEGER,
    recommended_price INTEGER,
    source_url TEXT NOT NULL DEFAULT '',
    source_modified TEXT NOT NULL DEFAULT '',
    UNIQUE(item_id, source, observed_on, source_url)
);
CREATE TABLE IF NOT EXISTS source_checks (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    checked_on TEXT NOT NULL,
    status TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    source_modified TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(item_id, source)
);
CREATE INDEX IF NOT EXISTS price_observations_item_source_date
ON price_observations(item_id, source, observed_on DESC);
PRAGMA user_version = 1;
"""

PRESERVED_PRICE_COLUMNS = (
    "Suggested Price",
    "Market Low",
    "Market High",
    "Pricing Source",
    "Pricing Notes",
    "Price Checked",
    "NukaTrader Low",
    "NukaTrader High",
    "NukaTrader Recommended",
    "NukaTrader URL",
    "NukaTrader Checked",
    "NukaTrader Source Modified",
)


class TextExtractor(HTMLParser):
    """Collect visible text from a NukaTrader item page."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def item_type(item: dict[str, Any]) -> str:
    """Decode the category bits used by Invent-O-Matic's raw JSON export."""
    name = str(item.get("text", ""))
    flag = int(item.get("filterFlag", 0) or 0)
    if name.casefold().startswith(("plan: ", "recipe: ")):
        return "NOTES"
    for bit, category in (
        (131072, "HOLO"), (65536, "AMMO"), (32768, "MODS"),
        (16384, "JUNK"), (524288, "MISC"), (8192, "MISC"),
        (2048, "NOTES"), (1024, "NOTES"), (64, "AID"),
        (32, "FOOD_WATER"), (16, "APPAREL"), (8, "ARMOR"), (4, "WEAPON"),
    ):
        if flag & bit:
            return category
    if float(item.get("damage", 0) or 0) > 0:
        return "WEAPON"
    return "UNKNOWN"


def lookup_name(name: str) -> str:
    """Remove user display markers while preserving the exact name separately."""
    normalized = re.sub(r"^[#\u00ac]+\s*", "", name.strip())
    return re.sub(r"\s*\u00a2$", "", normalized).strip()


def exact_regex(name: str) -> str:
    """Create an anchored ActionScript-compatible literal-name regex."""
    escaped = re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", name)
    return f"^{escaped}$"


def listed_vendor_price(item: dict[str, Any]) -> int | None:
    """Read a listing price from either legacy or Invent-O-Matic 2.8 fields."""
    vending = item.get("vendingData") or {}
    vending_price = int(vending.get("price", 0) or 0)
    if vending_price and (
        vending.get("isVendedOnOtherMachine", False)
        or int(vending.get("machineType", 0) or 0) == 1
    ):
        return vending_price
    if item.get("isOffered", False):
        return int(item.get("offerValue", 0) or 0)
    return None


def legendary_effects(item: dict[str, Any]) -> str:
    if not item.get("isLegendary", False):
        return ""
    descriptions = [
        str(entry.get("value", ""))
        for entry in item.get("ItemCardEntries") or []
        if entry.get("text") == "DESC" and entry.get("value")
    ]
    text = " | ".join(descriptions).replace("\u00ac", "")
    return " | ".join(part.strip() for part in text.splitlines() if part.strip())


def load_dump(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read Invent-O-Matic dump {path}: {error}") from error
    if not isinstance(data.get("characterInventories"), dict):
        raise ValueError("input has no characterInventories object")
    return data


def optional_integer(value: str | int | None) -> int | None:
    text = str(value or "").strip()
    return int(text) if text else None


def item_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return tuple(str(row.get(column, "") or "") for column in (
        "Item Name", "Category", "Level", "Legendary Stars", "Legendary Effects"
    ))


@contextmanager
def open_price_database(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(DATABASE_SCHEMA)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def database_item_id(connection: sqlite3.Connection, row: dict[str, Any]) -> int:
    identity = item_identity(row)
    connection.execute(
        """
        INSERT INTO items (
            item_name, lookup_name, category, level, legendary_stars, legendary_effects
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_name, category, level, legendary_stars, legendary_effects)
        DO UPDATE SET lookup_name = excluded.lookup_name
        """,
        (identity[0], row.get("Price Lookup Name", ""), *identity[1:]),
    )
    result = connection.execute(
        """
        SELECT id FROM items
        WHERE item_name = ? AND category = ? AND level = ?
          AND legendary_stars = ? AND legendary_effects = ?
        """,
        identity,
    ).fetchone()
    if result is None:
        raise ValueError(f"could not store database item {identity[0]!r}")
    return int(result["id"])


def history_item_id(connection: sqlite3.Connection, row: dict[str, Any]) -> int:
    result = connection.execute(
        "SELECT id FROM items WHERE item_name = ? AND category = ? ORDER BY id LIMIT 1",
        (row.get("Item Name", ""), row.get("Category", "")),
    ).fetchone()
    if result is not None:
        return int(result["id"])
    item = {column: "" for column in CSV_COLUMNS}
    item.update({
        "Item Name": row.get("Item Name", ""),
        "Price Lookup Name": row.get("Price Lookup Name", ""),
        "Category": row.get("Category", ""),
    })
    return database_item_id(connection, item)


def upsert_source_check(
    connection: sqlite3.Connection,
    item_id: int,
    source: str,
    checked_on: str,
    status: str,
    source_url: str = "",
    source_modified: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO source_checks (
            item_id, source, checked_on, status, source_url, source_modified
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id, source) DO UPDATE SET
            checked_on = excluded.checked_on,
            status = excluded.status,
            source_url = excluded.source_url,
            source_modified = excluded.source_modified
        WHERE excluded.checked_on >= source_checks.checked_on
        """,
        (item_id, source, checked_on, status, source_url, source_modified),
    )


def insert_observation(
    connection: sqlite3.Connection,
    item_id: int,
    source: str,
    observed_on: str,
    low: int | None,
    high: int | None,
    recommended: int | None,
    source_url: str,
    source_modified: str,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO price_observations (
            item_id, source, observed_on, market_low, market_high,
            recommended_price, source_url, source_modified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id, source, observed_on, low, high, recommended,
            source_url, source_modified,
        ),
    )


def sync_price_database(csv_path: Path, history_path: Path, database_path: Path) -> None:
    """Bootstrap SQLite or explicitly import reviewed CSV and history data."""
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)

    history_rows: list[dict[str, str]] = []
    if history_path.exists():
        with history_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if set(reader.fieldnames or []) != set(PRICE_HISTORY_COLUMNS):
                raise ValueError(f"history CSV columns do not match {history_path}")
            history_rows = list(reader)

    with open_price_database(database_path) as connection:
        for row in rows:
            item_id = database_item_id(connection, row)
            suggested = optional_integer(row.get("Suggested Price"))
            if suggested is not None:
                incoming_checked = (row.get("Price Checked") or "").strip()
                current = connection.execute(
                    "SELECT price_checked FROM approved_prices WHERE item_id = ?",
                    (item_id,),
                ).fetchone()
                if current is None or incoming_checked >= current["price_checked"]:
                    connection.execute(
                        """
                        INSERT INTO approved_prices (
                            item_id, suggested_price, market_low, market_high,
                            pricing_source, pricing_notes, price_checked
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(item_id) DO UPDATE SET
                            suggested_price = excluded.suggested_price,
                            market_low = excluded.market_low,
                            market_high = excluded.market_high,
                            pricing_source = excluded.pricing_source,
                            pricing_notes = excluded.pricing_notes,
                            price_checked = excluded.price_checked
                        """,
                        (
                            item_id, suggested,
                            optional_integer(row.get("Market Low")),
                            optional_integer(row.get("Market High")),
                            row.get("Pricing Source", ""),
                            row.get("Pricing Notes", ""),
                            incoming_checked,
                        ),
                    )
            nuka_checked = (row.get("NukaTrader Checked") or "").strip()
            nuka_recommended = optional_integer(row.get("NukaTrader Recommended"))
            if nuka_checked:
                nuka_url = row.get("NukaTrader URL", "")
                nuka_modified = row.get("NukaTrader Source Modified", "")
                if nuka_recommended is not None:
                    insert_observation(
                        connection, item_id, "NukaTrader", nuka_checked,
                        optional_integer(row.get("NukaTrader Low")),
                        optional_integer(row.get("NukaTrader High")),
                        nuka_recommended, nuka_url, nuka_modified,
                    )
                upsert_source_check(
                    connection, item_id, "NukaTrader", nuka_checked,
                    "market" if nuka_recommended is not None else "no_market_price",
                    nuka_url, nuka_modified,
                )

        for row in history_rows:
            item_id = history_item_id(connection, row)
            insert_observation(
                connection, item_id, row.get("Source", ""),
                row.get("Observed On", ""),
                optional_integer(row.get("Market Low")),
                optional_integer(row.get("Market High")),
                optional_integer(row.get("Recommended Price")),
                row.get("Source URL", ""),
                row.get("Source Modified", ""),
            )
            if row.get("Source") == "NukaTrader":
                upsert_source_check(
                    connection, item_id, "NukaTrader", row.get("Observed On", ""),
                    "market", row.get("Source URL", ""),
                    row.get("Source Modified", ""),
                )

        item_count = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        price_count = connection.execute(
            "SELECT COUNT(*) FROM approved_prices"
        ).fetchone()[0]
        observation_count = connection.execute(
            "SELECT COUNT(*) FROM price_observations"
        ).fetchone()[0]
    print(
        f"Synced {item_count} items, {price_count} approved prices, and "
        f"{observation_count} observations to {database_path}"
    )


def load_database_prices(database_path: Path) -> dict[tuple[str, ...], dict[str, str]]:
    if not database_path.exists():
        return {}
    prices: dict[tuple[str, ...], dict[str, str]] = {}
    with open_price_database(database_path) as connection:
        records = connection.execute(
            """
            SELECT i.*, p.*, c.checked_on AS nuka_checked,
                   c.source_url AS nuka_url, c.source_modified AS nuka_modified,
                   o.market_low AS nuka_low, o.market_high AS nuka_high,
                   o.recommended_price AS nuka_recommended
            FROM items i
            JOIN approved_prices p ON p.item_id = i.id
            LEFT JOIN source_checks c
              ON c.item_id = i.id AND c.source = 'NukaTrader'
            LEFT JOIN price_observations o
              ON o.item_id = i.id AND o.source = 'NukaTrader'
             AND o.observed_on = c.checked_on AND o.source_url = c.source_url
            """
        ).fetchall()
        for record in records:
            key = (
                record["item_name"], record["category"], record["level"],
                record["legendary_stars"], record["legendary_effects"],
            )
            prices[key] = {
                "Suggested Price": str(record["suggested_price"]),
                "Market Low": "" if record["market_low"] is None else str(record["market_low"]),
                "Market High": "" if record["market_high"] is None else str(record["market_high"]),
                "Pricing Source": record["pricing_source"],
                "Pricing Notes": record["pricing_notes"],
                "Price Checked": record["price_checked"],
                "NukaTrader Low": "" if record["nuka_low"] is None else str(record["nuka_low"]),
                "NukaTrader High": "" if record["nuka_high"] is None else str(record["nuka_high"]),
                "NukaTrader Recommended": (
                    "" if record["nuka_recommended"] is None
                    else str(record["nuka_recommended"])
                ),
                "NukaTrader URL": record["nuka_url"] or "",
                "NukaTrader Checked": record["nuka_checked"] or "",
                "NukaTrader Source Modified": record["nuka_modified"] or "",
            }
    return prices


def normalize(
    input_path: Path, output_path: Path, database_path: Path | None = None
) -> None:
    data = load_dump(input_path)
    existing_prices: dict[tuple[str, ...], dict[str, str]] = {}
    if output_path.exists():
        with output_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                key = (
                    row.get("Item Name", ""),
                    row.get("Category", ""),
                    row.get("Level", ""),
                    row.get("Legendary Stars", ""),
                    row.get("Legendary Effects", ""),
                )
                existing_prices[key] = row
    database_prices = load_database_prices(database_path) if database_path else {}
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for character, inventory in data["characterInventories"].items():
        for source_key, source_name in (
            ("playerInventory", "PLAYER"), ("stashInventory", "STASH"),
        ):
            for item in inventory.get(source_key, []):
                if not item.get("isTradable", False):
                    continue
                exact_name = str(item.get("text", "")).strip()
                if not exact_name:
                    continue
                row = dict(item)
                row["_character"] = character
                row["_source"] = source_name
                row["_type"] = item_type(item)
                row["_effects"] = legendary_effects(item)
                key = (
                    exact_name, row["_type"], int(item.get("itemLevel", 0) or 0),
                    int(item.get("numLegendaryStars", 0) or 0), row["_effects"],
                )
                grouped[key].append(row)

    rows: list[dict[str, str | int]] = []
    for (name, category, level, stars, effects), items in grouped.items():
        listed_prices = sorted({
            price for item in items
            if (price := listed_vendor_price(item)) is not None
        })
        listed_quantity = sum(
            int(item.get("count", 0) or 0)
            for item in items if listed_vendor_price(item) is not None
        )
        sources = sorted({item["_source"] for item in items})
        rows.append({
            "Item Name": name,
            "Price Lookup Name": lookup_name(name),
            "Category": category,
            "Available Quantity": sum(int(item.get("count", 0) or 0) for item in items),
            "Quantity to Sell": listed_quantity or "",
            "Suggested Price": "",
            "Source": "+".join(sources),
            "Already Listed": "YES" if listed_prices else "NO",
            "Current Listed Quantity": listed_quantity or "",
            "Current Vendor Price": "/".join(map(str, listed_prices)),
            "Market Low": "",
            "Market High": "",
            "Pricing Source": "",
            "Pricing Notes": "",
            "Price Checked": "",
            "NukaTrader Low": "",
            "NukaTrader High": "",
            "NukaTrader Recommended": "",
            "NukaTrader URL": "",
            "NukaTrader Checked": "",
            "NukaTrader Source Modified": "",
            "Level": level or "",
            "Legendary Stars": stars or "",
            "Legendary Effects": effects,
        })

    rows.sort(key=lambda row: (str(row["Category"]), str(row["Item Name"]).casefold()))
    for row in rows:
        key = item_identity(row)
        previous = existing_prices.get(key)
        stored = database_prices.get(key)
        primary = stored or previous
        if primary:
            for column in PRESERVED_PRICE_COLUMNS[:6]:
                row[column] = primary.get(column, "")
        nuka = stored or previous
        if nuka:
            for column in PRESERVED_PRICE_COLUMNS[6:]:
                row[column] = nuka.get(column, "")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} tradable item rows to {output_path}")


def select_unlocked(input_path: Path, csv_path: Path) -> None:
    """Select the full quantity of every tradable, transfer-unlocked item."""
    data = load_dump(input_path)
    unlocked: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    for inventory in data["characterInventories"].values():
        for source_key in ("playerInventory", "stashInventory"):
            for item in inventory.get(source_key, []):
                if not item.get("isTradable", False) or item.get("isTransferLocked", False):
                    continue
                name = str(item.get("text", "")).strip()
                if not name:
                    continue
                if (
                    name.casefold().startswith(("plan: ", "recipe: "))
                    and not item.get("isLearnedRecipe", False)
                ):
                    continue
                key = (
                    name,
                    item_type(item),
                    str(int(item.get("itemLevel", 0) or 0) or ""),
                    str(int(item.get("numLegendaryStars", 0) or 0) or ""),
                    legendary_effects(item),
                )
                unlocked[key] += int(item.get("count", 0) or 0)

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)

    selected = 0
    for row in rows:
        key = tuple(row.get(column, "") for column in (
            "Item Name", "Category", "Level", "Legendary Stars", "Legendary Effects"
        ))
        quantity = unlocked.get(key, 0)
        row["Quantity to Sell"] = str(quantity) if quantity else ""
        selected += bool(quantity)

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Selected all unlocked quantities for {selected} rows in {csv_path}")


def nukatrader_slug(name: str) -> str:
    """Convert an inventory lookup name to NukaTrader's URL slug style."""
    name = re.sub(r"^(?:Plan|Recipe):\s*", "", name, flags=re.IGNORECASE)
    name = name.replace("'", "")
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")
    except (HTTPError, URLError) as error:
        raise ValueError(f"cannot fetch {url}: {error}") from error


def parse_nukatrader_page(page: str) -> tuple[int, int, int, str] | None:
    """Return low, high, recommended, and source-modified values when present."""
    extractor = TextExtractor()
    extractor.feed(page)
    text = extractor.text()

    def price(*patterns: str) -> int | None:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", ""))
        return None

    low = price(r"Price Low\s+([\d,]+)\s+caps", r"Low:?\s+([\d,]+)\s+caps")
    high = price(r"Price High\s+([\d,]+)\s+caps", r"High:?\s+([\d,]+)\s+caps")
    recommended = price(
        r"Estimated Value\s+([\d,]+)\s+caps",
        r"Recommended:?\s+([\d,]+)\s+caps",
    )
    if low is None or high is None or recommended is None:
        return None
    modified_match = re.search(r'"dateModified":"([^"T]+)', page)
    source_modified = modified_match.group(1) if modified_match else ""
    return low, high, recommended, source_modified


def apply_nukatrader_price(
    row: dict[str, str], low: int, high: int, recommended: int,
    url: str, checked_on: str, source_modified: str,
) -> None:
    row.update({
        "NukaTrader Low": str(low),
        "NukaTrader High": str(high),
        "NukaTrader Recommended": str(recommended),
        "NukaTrader URL": url,
        "NukaTrader Checked": checked_on,
        "NukaTrader Source Modified": source_modified,
    })
    if (
        (row.get("Pricing Source") or "").strip() == "NukaTrader"
        or not (row.get("Suggested Price") or "").strip()
    ):
        row.update({
            "Suggested Price": str(recommended),
            "Market Low": str(low),
            "Market High": str(high),
            "Pricing Source": "NukaTrader",
            "Pricing Notes": url,
            "Price Checked": checked_on,
        })


def refresh_nukatrader(
    csv_path: Path,
    history_path: Path,
    database_path: Path,
    checked_on: str,
    max_age_days: int = DEFAULT_PRICE_MAX_AGE_DAYS,
    force: bool = False,
) -> None:
    """Use recent SQLite prices and refresh stale NukaTrader checks online."""
    try:
        checked_date = date.fromisoformat(checked_on)
    except ValueError as error:
        raise ValueError("--date must use YYYY-MM-DD format") from error
    if max_age_days < 0:
        raise ValueError("--max-age-days must be zero or greater")

    if not database_path.exists():
        sync_price_database(csv_path, history_path, database_path)

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"Item Name", "Price Lookup Name", "Category", "Suggested Price"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)

    observations: list[dict[str, str | int]] = []
    cached = 0
    refreshed = 0
    without_market_price = 0
    not_found = 0
    cutoff = checked_date - timedelta(days=max_age_days)
    stale: list[tuple[dict[str, str], int, str]] = []

    with open_price_database(database_path) as connection:
        for row in rows:
            category = (row.get("Category") or "").upper()
            route = NUKATRADER_ROUTES.get(category)
            if not route:
                continue
            item_id = database_item_id(connection, row)
            check = connection.execute(
                """
                SELECT checked_on, status, source_url, source_modified
                FROM source_checks WHERE item_id = ? AND source = 'NukaTrader'
                """,
                (item_id,),
            ).fetchone()
            recent = False
            if check is not None and not force:
                try:
                    recent = date.fromisoformat(check["checked_on"]) > cutoff
                except ValueError:
                    recent = False
            if recent:
                cached += 1
                if check["status"] == "market":
                    price = connection.execute(
                        """
                        SELECT market_low, market_high, recommended_price,
                               source_url, source_modified, observed_on
                        FROM price_observations
                        WHERE item_id = ? AND source = 'NukaTrader'
                        ORDER BY observed_on DESC, id DESC LIMIT 1
                        """,
                        (item_id,),
                    ).fetchone()
                    if price is not None and price["recommended_price"] is not None:
                        apply_nukatrader_price(
                            row, int(price["market_low"]), int(price["market_high"]),
                            int(price["recommended_price"]), price["source_url"],
                            price["observed_on"], price["source_modified"],
                        )
                continue
            stale.append((row, item_id, route))

        catalog: dict[str, str] = {}
        if stale:
            for sitemap in NUKATRADER_SITEMAPS:
                for url in re.findall(r"<loc>([^<]+)</loc>", fetch_text(sitemap)):
                    catalog[urlparse(url).path.rstrip("/")] = url

        for row, item_id, route in stale:
            category = (row.get("Category") or "").upper()
            path = f"/{route}/{nukatrader_slug(row.get('Price Lookup Name') or '')}"
            url = catalog.get(path)
            if not url:
                not_found += 1
                upsert_source_check(
                    connection, item_id, "NukaTrader", checked_on, "not_found"
                )
                continue
            parsed = parse_nukatrader_page(fetch_text(url))
            if parsed is None:
                without_market_price += 1
                upsert_source_check(
                    connection, item_id, "NukaTrader", checked_on,
                    "no_market_price", url,
                )
                continue
            low, high, recommended, source_modified = parsed
            refreshed += 1
            insert_observation(
                connection, item_id, "NukaTrader", checked_on,
                low, high, recommended, url, source_modified,
            )
            upsert_source_check(
                connection, item_id, "NukaTrader", checked_on,
                "market", url, source_modified,
            )
            apply_nukatrader_price(
                row, low, high, recommended, url, checked_on, source_modified
            )
            observations.append({
                "Observed On": checked_on,
                "Item Name": row.get("Item Name", ""),
                "Price Lookup Name": row.get("Price Lookup Name", ""),
                "Category": category,
                "Source": "NukaTrader",
                "Market Low": low,
                "Market High": high,
                "Recommended Price": recommended,
                "Source URL": url,
                "Source Modified": source_modified,
            })

    existing_history: list[dict[str, str]] = []
    existing_keys: set[tuple[str, str, str]] = set()
    if history_path.exists():
        with history_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if set(reader.fieldnames or []) != set(PRICE_HISTORY_COLUMNS):
                raise ValueError(f"history CSV columns do not match {history_path}")
            existing_history = list(reader)
        existing_keys = {
            (row["Observed On"], row["Item Name"], row["Source URL"])
            for row in existing_history
        }
    new_observations = [
        row for row in observations
        if (str(row["Observed On"]), str(row["Item Name"]), str(row["Source URL"]))
        not in existing_keys
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=PRICE_HISTORY_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(existing_history)
        writer.writerows(new_observations)
    sync_price_database(csv_path, history_path, database_path)
    print(
        f"Used {cached} recent SQLite NukaTrader checks; refreshed {refreshed} "
        f"market prices online; {without_market_price} pages had NPC-only or no "
        f"market prices; {not_found} items were not in the catalog; appended "
        f"{len(new_observations)} observations to {history_path}"
    )


def parse_integer(value: str, field: str, line: int) -> int:
    try:
        return int(value.strip())
    except ValueError as error:
        raise ValueError(f"line {line}: {field} must be a whole number") from error


def build_config(csv_path: Path, config_path: Path, output_path: Path) -> None:
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"Item Name", "Category", "Quantity to Sell", "Suggested Price"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        entries: list[dict[str, Any]] = []
        selected_names: set[str] = set()
        default_vendor_prices: dict[str, int] = {}
        for line, row in enumerate(reader, start=2):
            quantity_text = (row.get("Quantity to Sell") or "").strip()
            price_text = (row.get("Suggested Price") or "").strip()
            name = (row.get("Item Name") or "").strip()
            if price_text:
                if not name:
                    raise ValueError(f"line {line}: Item Name is empty")
                default_price = parse_integer(price_text, "Suggested Price", line)
                if not 0 <= default_price <= 40000:
                    raise ValueError(f"line {line}: Suggested Price must be between 0 and 40000")
                default_vendor_prices[exact_regex(name)] = default_price
            if not quantity_text:
                continue
            if not price_text:
                raise ValueError(f"line {line}: Suggested Price is required when quantity is selected")
            category = (row.get("Category") or "").strip().upper()
            quantity = parse_integer(quantity_text, "Quantity to Sell", line)
            price = parse_integer(price_text, "Suggested Price", line)
            if not name:
                raise ValueError(f"line {line}: Item Name is empty")
            if category not in VALID_TYPES:
                raise ValueError(f"line {line}: unsupported category {category!r}")
            if quantity < 0:
                raise ValueError(f"line {line}: use 0 for all or a positive Quantity to Sell")
            available_text = (row.get("Available Quantity") or "").strip()
            if available_text and quantity > parse_integer(available_text, "Available Quantity", line):
                raise ValueError(f"line {line}: Quantity to Sell exceeds Available Quantity")
            if not 0 <= price <= 40000:
                raise ValueError(f"line {line}: Suggested Price must be between 0 and 40000")
            if name in selected_names:
                raise ValueError(
                    f"line {line}: {name!r} is selected more than once; EXACT cannot distinguish duplicates"
                )
            selected_names.add(name)
            entries.append({
                "enabled": True,
                "matchMode": "EXACT",
                "itemNames": [name],
                "types": [category],
                "amount": quantity,
                "price": price,
                "_alreadyListed": (row.get("Already Listed") or "").strip() == "YES",
            })

    if not entries:
        raise ValueError("no priced rows found; fill Quantity to Sell and Suggested Price first")

    if len(entries) > VENDOR_SLOT_LIMIT:
        entries.sort(
            key=lambda entry: (
                0 if "Bobblehead" in entry["itemNames"][0] else
                1 if entry["itemNames"][0].startswith(("Plan: ", "Recipe: ")) else
                2 if entry["_alreadyListed"] else 3,
                -entry["price"],
                entry["itemNames"][0].casefold(),
            )
        )
        omitted = entries[VENDOR_SLOT_LIMIT:]
        entries = entries[:VENDOR_SLOT_LIMIT]
        print(
            f"Vendor limit is {VENDOR_SLOT_LIMIT}; omitted {len(omitted)} lowest-priority entries:",
            file=sys.stderr,
        )
        for entry in omitted:
            print(
                f"  - {entry['itemNames'][0]} ({entry['price']} caps)",
                file=sys.stderr,
            )
    for entry in entries:
        del entry["_alreadyListed"]
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read config {config_path}: {error}") from error

    config["defaultVendorItemPrice"] = {
        "enabled": True,
        "defaultValue": -1,
        "itemNames": default_vendor_prices,
        "categoryNames": {},
    }

    camp = config.setdefault("campAssignConfig", {})
    camp["enabled"] = True
    configs = camp.setdefault("configs", [])
    generated = {
        "name": "AutoVend Priced Sheet", "enabled": True, "debug": True,
        "showButton": True, "showMessage": True, "assignMode": "VENDOR",
        "hotkey": "F", "delay": ASSIGN_DELAY_MS, "configs": entries,
    }
    for index, existing in enumerate(configs):
        if existing.get("name") == generated["name"]:
            configs[index] = generated
            break
    else:
        configs.append(generated)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"Wrote {len(entries)} AutoVend entries and "
        f"{len(default_vendor_prices)} default vendor prices to {output_path}"
    )


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description=__doc__)
    commands = main.add_subparsers(dest="command", required=True)
    normalize_parser = commands.add_parser("normalize", help="create a pricing CSV")
    normalize_parser.add_argument("input", type=Path)
    normalize_parser.add_argument("output", type=Path)
    normalize_parser.add_argument("--database", type=Path)
    sync_parser = commands.add_parser(
        "sync-db", help="bootstrap SQLite or import reviewed CSV changes"
    )
    sync_parser.add_argument("csv", type=Path)
    sync_parser.add_argument("history", type=Path)
    sync_parser.add_argument("database", type=Path)
    select_parser = commands.add_parser(
        "select-unlocked",
        help="select all tradable and transfer-unlocked inventory quantities",
    )
    select_parser.add_argument("input", type=Path)
    select_parser.add_argument("csv", type=Path)
    build_parser = commands.add_parser("build", help="create a config from a priced CSV")
    build_parser.add_argument("csv", type=Path)
    build_parser.add_argument("config", type=Path)
    build_parser.add_argument("output", type=Path)
    nuka_parser = commands.add_parser(
        "refresh-nukatrader",
        help="refresh NukaTrader reference prices and append source history",
    )
    nuka_parser.add_argument("csv", type=Path)
    nuka_parser.add_argument("history", type=Path)
    nuka_parser.add_argument("database", type=Path)
    nuka_parser.add_argument("--date", default=date.today().isoformat())
    nuka_parser.add_argument(
        "--max-age-days", type=int, default=DEFAULT_PRICE_MAX_AGE_DAYS
    )
    nuka_parser.add_argument("--force", action="store_true")
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "normalize":
            normalize(args.input, args.output, args.database)
        elif args.command == "sync-db":
            sync_price_database(args.csv, args.history, args.database)
        elif args.command == "select-unlocked":
            select_unlocked(args.input, args.csv)
        elif args.command == "build":
            build_config(args.csv, args.config, args.output)
        else:
            refresh_nukatrader(
                args.csv, args.history, args.database, args.date,
                args.max_age_days, args.force,
            )
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

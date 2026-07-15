#!/usr/bin/env python3
"""Normalize an Invent-O-Matic dump and build a safe AutoVend config."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
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


def normalize(input_path: Path, output_path: Path) -> None:
    data = load_dump(input_path)
    existing_prices: dict[tuple[str, str, str, str], dict[str, str]] = {}
    if output_path.exists():
        with output_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                key = (
                    row.get("Item Name", ""),
                    row.get("Category", ""),
                    row.get("Level", ""),
                    row.get("Legendary Stars", ""),
                )
                existing_prices[key] = row
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
        key = tuple(str(row[column]) for column in (
            "Item Name", "Category", "Level", "Legendary Stars"
        ))
        previous = existing_prices.get(key)
        if previous:
            for column in PRESERVED_PRICE_COLUMNS:
                row[column] = previous.get(column, "")
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


def refresh_nukatrader(csv_path: Path, history_path: Path, checked_on: str) -> None:
    """Refresh NukaTrader observations without replacing other pricing sources."""
    try:
        date.fromisoformat(checked_on)
    except ValueError as error:
        raise ValueError("--date must use YYYY-MM-DD format") from error

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"Item Name", "Price Lookup Name", "Category", "Suggested Price"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)

    catalog: dict[str, str] = {}
    for sitemap in NUKATRADER_SITEMAPS:
        for url in re.findall(r"<loc>([^<]+)</loc>", fetch_text(sitemap)):
            catalog[urlparse(url).path.rstrip("/")] = url

    observations: list[dict[str, str | int]] = []
    matched = 0
    without_market_price = 0
    for row in rows:
        category = (row.get("Category") or "").upper()
        route = NUKATRADER_ROUTES.get(category)
        if not route:
            continue
        path = f"/{route}/{nukatrader_slug(row.get('Price Lookup Name') or '')}"
        url = catalog.get(path)
        if not url:
            continue
        parsed = parse_nukatrader_page(fetch_text(url))
        if parsed is None:
            without_market_price += 1
            continue
        low, high, recommended, source_modified = parsed
        matched += 1
        row.update({
            "NukaTrader Low": str(low),
            "NukaTrader High": str(high),
            "NukaTrader Recommended": str(recommended),
            "NukaTrader URL": url,
            "NukaTrader Checked": checked_on,
            "NukaTrader Source Modified": source_modified,
        })
        if (row.get("Pricing Source") or "").strip() == "NukaTrader":
            row.update({
                "Suggested Price": str(recommended),
                "Market Low": str(low),
                "Market High": str(high),
                "Pricing Notes": url,
                "Price Checked": checked_on,
            })
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
    print(
        f"Refreshed {matched} NukaTrader market prices in {csv_path}; "
        f"{without_market_price} matched pages had NPC-only or no market prices; "
        f"appended {len(new_observations)} observations to {history_path}"
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
    nuka_parser.add_argument("--date", default=date.today().isoformat())
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "normalize":
            normalize(args.input, args.output)
        elif args.command == "select-unlocked":
            select_unlocked(args.input, args.csv)
        elif args.command == "build":
            build_config(args.csv, args.config, args.output)
        else:
            refresh_nukatrader(args.csv, args.history, args.date)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

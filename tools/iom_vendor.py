#!/usr/bin/env python3
"""Normalize an Invent-O-Matic dump and build a safe AutoVend config."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


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
    "Level",
    "Legendary Stars",
    "Legendary Effects",
]

VALID_TYPES = {
    "WEAPON", "ARMOR", "APPAREL", "FOOD_WATER", "AID", "NOTES",
    "HOLO", "AMMO", "MISC", "MODS", "JUNK",
}

VENDOR_SLOT_LIMIT = 120
ASSIGN_DELAY_MS = 2000

PRESERVED_PRICE_COLUMNS = (
    "Suggested Price",
    "Market Low",
    "Market High",
    "Pricing Source",
    "Pricing Notes",
    "Price Checked",
)


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
            int(item.get("offerValue", 0) or 0)
            for item in items if item.get("isOffered", False)
        })
        listed_quantity = sum(
            int(item.get("count", 0) or 0)
            for item in items if item.get("isOffered", False)
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
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} tradable item rows to {output_path}")


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
            })

    if not entries:
        raise ValueError("no priced rows found; fill Quantity to Sell and Suggested Price first")

    if len(entries) > VENDOR_SLOT_LIMIT:
        entries.sort(
            key=lambda entry: (
                0 if entry["itemNames"][0].startswith("Plan: ") else 1,
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
    build_parser = commands.add_parser("build", help="create a config from a priced CSV")
    build_parser.add_argument("csv", type=Path)
    build_parser.add_argument("config", type=Path)
    build_parser.add_argument("output", type=Path)
    return main


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "normalize":
            normalize(args.input, args.output)
        else:
            build_config(args.csv, args.config, args.output)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

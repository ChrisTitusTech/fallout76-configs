# Vendor Pricing Specification

## Purpose

Maintain repeatable Fallout 76 player-vendor prices from the latest live
Invent-O-Matic export, prior price observations, and named market references.
The process must preserve exact in-game item names, retain source history, and
produce a reviewable configuration that never exceeds 120 vendor slots.

## Source files

- `itemsmod.ini` is the untouched live Invent-O-Matic Extractor export.
- `itemsmod-pricing.csv` is the current inventory and approved price sheet.
- `item-prices.sqlite3` is the canonical local item and price cache.
- `item-price-history.csv` is an append-only record of external price
  observations. Do not rewrite past observations.
- `mods/Data/inventOmaticStashConfig.json` is the unpriced base configuration.
- `mods/Data/inventOmaticStashConfig.priced.json` is generated output.

## Pricing evidence

Use evidence in this order:

1. Confirmed sales and time-to-sale recorded by the player.
2. Repeated observations in `item-price-history.csv`.
3. Current community trading reports for the PC market.
4. NukaTrader low, high, and recommended values.
5. A documented category baseline when item-specific evidence is unavailable.

NukaTrader is a required comparison source for item types it catalogs. Its
catalog currently covers plans, apparel, and components. Some catalog pages
contain only an NPC vendor price; those are not player-market observations and
must not update the price sheet. Record the page's `dateModified` value because
many valuations may be older than the day they are checked.

A NukaTrader observation updates the dedicated `NukaTrader *` columns and the
append-only history. It becomes `Suggested Price` when no approved database or
CSV price exists, and updates `Suggested Price` when the active source is already
NukaTrader. It must not silently replace an existing community or confirmed-sale
price.

## SQLite data model

The local database uses four tables:

- `items` stores exact item identity: item name, lookup name, category, level,
  legendary stars, and legendary effects.
- `approved_prices` stores the current reviewed vendor value and its primary
  source metadata.
- `price_observations` stores append-only dated values from NukaTrader,
  Market76, and future sources.
- `source_checks` stores the latest check status for each item and source,
  including `market`, `no_market_price`, and `not_found` results.

The database is the primary price source and remains durable when an item is
absent from the current CSV inventory. CSV remains the human-review surface,
but routine normalization never replaces an existing database price from CSV.
It uses the prior CSV row only when the exact item has no approved database
price. `sync-db` is reserved for bootstrapping a missing database or explicitly
importing reviewed CSV changes; during that explicit import, only an equally or
more recently checked CSV value may replace the stored approved value.

NukaTrader checks have a 30-day time-to-live. A check less than 30 days old must
be used without network access. At 30 days, only stale or previously unseen
items are checked online; fresh items are loaded from SQLite. `--force` bypasses
the cache for deliberate full refreshes.

## History-based price decisions

- Do not infer a sale from an item disappearing between inventory exports. It
  may have been consumed, dropped, transferred, scrapped, or moved.
- When a listing is confirmed sold within 24 hours on two consecutive cycles,
  consider increasing the price by 10 percent, capped at the supported market
  high.
- When a listing is confirmed unsold for at least seven active vendor days,
  consider decreasing it by 10 to 20 percent, normally no lower than the most
  recent supported market low.
- With no confirmed sale history, preserve the approved price unless at least
  two recent sources support a change.
- Treat a source page modified more than 180 days ago as a reference, not sole
  justification for changing an active price.
- Round ordinary prices to convenient player-vendor increments. Preserve exact
  source recommendations when the row explicitly uses that source.
- Never place a trade-only item in the vendor merely because the game caps a
  listing at 40,000 caps. Keep its quantity blank until manually approved.

## Repeatable update procedure

Set the live Data directory if Fallout 76 is installed elsewhere:

```bash
export FALLOUT76_DATA_DIR="$HOME/.local/share/Steam/steamapps/common/Fallout76/Data"
```

### Normal in-game workflow

1. In Invent-O-Matic, run **Export from Inventory** to refresh the live
   `Data/itemsmod.ini` file.
2. Run `./tools/install_priced_config.sh` from the repository. The script
   performs the import, SQLite-first normalization, cache-aware NukaTrader
   refresh, unlocked-item selection, build, backup, installation, and
   byte-for-byte verification. It seeds SQLite from CSV only if the database is
   missing.
3. Move the items intended for sale into the character's **personal
   inventory**. AutoVend assigns from this inventory when the vendor opens.
4. Open the personal vendor and press **AutoVend**.

Transfer-locked items and unknown plans must remain protected throughout this
workflow. If more than 120 distinct items qualify, only the documented priority
batch is active and omitted items are reconsidered on the next run.

### Manual commands

Bootstrap SQLite or explicitly import reviewed CSV changes:

```bash
python3 tools/iom_vendor.py sync-db \
  itemsmod-pricing.csv item-price-history.csv item-prices.sqlite3
```

Copy in the newest export and normalize it. Normalization refreshes inventory
and listing quantities while restoring approved pricing and NukaTrader fields
from SQLite first. The existing CSV row is used only for an exact item missing
from the database:

```bash
cp -- "$FALLOUT76_DATA_DIR/itemsmod.ini" itemsmod.ini
python3 tools/iom_vendor.py normalize \
  itemsmod.ini itemsmod-pricing.csv --database item-prices.sqlite3
```

Refresh NukaTrader comparisons and append this check to price history:

```bash
python3 tools/iom_vendor.py refresh-nukatrader \
  itemsmod-pricing.csv item-price-history.csv item-prices.sqlite3 \
  --max-age-days 30
```

Select every tradable, transfer-unlocked quantity for AutoVend consideration.
Plans and recipes whose `isLearnedRecipe` flag is false must remain unselected:

```bash
python3 tools/iom_vendor.py select-unlocked itemsmod.ini itemsmod-pricing.csv
```

If more than 120 distinct rows are eligible, the generated configuration keeps
bobbleheads first, then known plans and recipes, existing listings, and finally
the highest-priced remaining items. Rows beyond the physical vendor limit
remain selected in the sheet so a later inventory cycle can reconsider them.

Review these fields before changing an approved price:

- `Current Vendor Price` and `Current Listed Quantity`
- `Suggested Price`, `Pricing Source`, `Pricing Notes`, and `Price Checked`
- the latest observations for the item in `item-price-history.csv`
- `NukaTrader Low`, `NukaTrader High`, `NukaTrader Recommended`, and
  `NukaTrader Source Modified`

After review, build and validate without installing:

```bash
python3 tools/iom_vendor.py build \
  itemsmod-pricing.csv \
  mods/Data/inventOmaticStashConfig.json \
  mods/Data/inventOmaticStashConfig.priced.json
python3 -m json.tool mods/Data/inventOmaticStashConfig.priced.json >/dev/null
python3 -m unittest discover -s tests -v
git diff --check
```

The normal installer repeats all manual refresh steps before installation. Run
it after reviewing the generated diff:

```bash
./tools/install_priced_config.sh
```

The installer must back up an existing live configuration and verify that the
installed file exactly matches the generated configuration.

## Acceptance criteria

- The pricing sheet represents the newest live export.
- SQLite contains every approved priced item, including items absent from the
  newest inventory export.
- Normal runs use an existing SQLite price before any CSV value and use CSV only
  when the exact item is missing from SQLite.
- Exact `Item Name` values remain unchanged by market lookups.
- Every online observation records its check date, source URL, and source
  modification date when available.
- Existing non-NukaTrader active prices are preserved by an automated
  NukaTrader refresh.
- NukaTrader checks less than 30 days old cause no online item or sitemap lookup.
- Missing checks and checks at least 30 days old are refreshed online.
- Stale refreshes request only stale or previously unseen supported items.
- The history file remains append-only and does not duplicate the same
  item/source observation for the same date.
- The generated configuration is valid JSON and contains no more than 120
  AutoVend entries.

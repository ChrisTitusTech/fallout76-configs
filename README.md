# Fallout 76 Custom INI for Linux

Path for custom INI is in compatdata 1151340:
```
steamapps/compatdata/1151340/pfx/drive_c/users/steamuser/My\ Documents/My\ Games/Fallout\ 76/Fallout76Custom.ini
```

## Invent-O-Matic vendor pricing

### In-game workflow

1. In Invent-O-Matic, run **Export from Inventory**. This writes the current
   inventory snapshot to the live Fallout 76 `Data/itemsmod.ini` file.
2. From this repository, run:

   ```bash
   ./tools/install_priced_config.sh
   ```

   The script imports the live export, preserves existing prices, refreshes
   NukaTrader history, selects unlocked tradable items and known plans, builds
   the 120-slot AutoVend batch, backs up the live JSON, and installs it.
3. In game, place the items you want to sell in your **personal inventory**.
   Keep protected items transfer locked; leave unknown plans protected.
4. Open your personal vendor and press **AutoVend**. AutoVend assigns matching
   personal-inventory items using the generated quantities and prices.

When more than 120 distinct items are eligible, the active batch prioritizes
bobbleheads, known plans and recipes, existing listings, and then the remaining
highest-priced items. Items outside the active batch are reconsidered the next
time the workflow runs.

### Pricing details

Keep `itemsmod.ini` as the untouched Invent-O-Matic Extractor JSON dump. Create
a spreadsheet-friendly file with:

```bash
python3 tools/iom_vendor.py normalize itemsmod.ini itemsmod-pricing.csv
```

Use `Price Lookup Name` when checking prices. Do not change `Item Name`: it is
the exact, case-sensitive name used by Invent-O-Matic. Every row has a reusable
`Suggested Price`; fill `Quantity to Sell` only for rows that should be listed.
Quantity `0` means the entire available stack. A blank quantity is skipped.

Running `normalize` again refreshes inventory and current-listing quantities
while preserving existing suggested prices and their source columns.

To select the full quantity of every tradable item that is not transfer locked,
while retaining unknown plans and recipes:

```bash
python3 tools/iom_vendor.py select-unlocked itemsmod.ini itemsmod-pricing.csv
```

When more than 120 distinct rows are eligible, the generated vendor keeps
bobbleheads first, then known plans and recipes, existing listings, and the
remaining highest-priced items until the 120-slot limit is reached.

Refresh NukaTrader reference prices and append the observations to the durable
price history with:

```bash
python3 tools/iom_vendor.py refresh-nukatrader \
  itemsmod-pricing.csv item-price-history.csv
```

This comparison covers NukaTrader plan, apparel, and component pages that have
player-market low, high, and recommended prices. It does not replace a newer
community price unless the row already names NukaTrader as its active source.
See [the vendor pricing specification](docs/VENDOR_PRICING_SPEC.md) for the
source priority, history-based adjustment rules, and complete update procedure.

Generate a reviewable config after pricing the sheet:

```bash
python3 tools/iom_vendor.py build \
  itemsmod-pricing.csv \
  mods/Data/inventOmaticStashConfig.json \
  mods/Data/inventOmaticStashConfig.priced.json
```

The generated `AutoVend Priced Sheet` uses exact matching, a 2,000 ms delay,
and debug mode. Review it, back up the live config, copy the generated file over
`inventOmaticStashConfig.json`, unassign existing vendor listings, then open
the vendor and run AutoVend.

The builder never emits more than Fallout 76's 120 vendor slots. If more than
120 rows are selected, it uses the priority described above and reports each
omitted row.

The builder also enables the Invent-O-Matic 2.8 `defaultVendorItemPrice`
feature and creates an anchored, escaped regular-expression rule for every
priced CSV row. This fills the sheet price automatically when assigning an
item manually from stash; quantities still need to be selected manually.

Import the current live export, refresh prices and selection, rebuild, back up,
install, and verify the live configuration in one command:

```bash
./tools/install_priced_config.sh
```

If Fallout 76 is in another Steam library, set `FALLOUT76_DATA_DIR` to its
`Data` directory before running the script.

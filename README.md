# Fallout 76 Custom INI for Linux

Path for custom INI is in compatdata 1151340:
```
steamapps/compatdata/1151340/pfx/drive_c/users/steamuser/My\ Documents/My\ Games/Fallout\ 76/Fallout76Custom.ini
```

## Invent-O-Matic vendor pricing

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
120 rows are selected, it keeps all selected `Plan:` rows first, then retains
the highest-priced remaining items, and reports each omitted row.

The builder also enables the Invent-O-Matic 2.8 `defaultVendorItemPrice`
feature and creates an anchored, escaped regular-expression rule for every
priced CSV row. This fills the sheet price automatically when assigning an
item manually from stash; quantities still need to be selected manually.

Rebuild, back up, install, and verify the live configuration in one command:

```bash
./tools/install_priced_config.sh
```

If Fallout 76 is in another Steam library, set `FALLOUT76_DATA_DIR` to its
`Data` directory before running the script.

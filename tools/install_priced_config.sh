#!/bin/sh

set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH='' cd -- "$script_dir/.." && pwd)

python_command=${PYTHON:-python3}
fallout_data_dir=${FALLOUT76_DATA_DIR:-"$HOME/.local/share/Steam/steamapps/common/Fallout76/Data"}

sheet="$repo_root/itemsmod-pricing.csv"
history="$repo_root/item-price-history.csv"
database="$repo_root/item-prices.sqlite3"
repo_export="$repo_root/itemsmod.ini"
base_config="$repo_root/mods/Data/inventOmaticStashConfig.json"
priced_config="$repo_root/mods/Data/inventOmaticStashConfig.priced.json"
live_config="$fallout_data_dir/inventOmaticStashConfig.json"
live_export="$fallout_data_dir/itemsmod.ini"

if ! command -v "$python_command" >/dev/null 2>&1; then
	printf 'error: Python command not found: %s\n' "$python_command" >&2
	exit 1
fi

if [ ! -d "$fallout_data_dir" ]; then
	printf 'error: Fallout 76 Data directory not found: %s\n' "$fallout_data_dir" >&2
	printf 'Set FALLOUT76_DATA_DIR if the Steam library is elsewhere.\n' >&2
	exit 1
fi

for required_file in "$live_export" "$sheet" "$base_config" "$script_dir/iom_vendor.py"; do
	if [ ! -f "$required_file" ]; then
		printf 'error: required file not found: %s\n' "$required_file" >&2
		exit 1
	fi
done

cp -- "$live_export" "$repo_export"
printf 'Imported live inventory export from %s\n' "$live_export"

if [ ! -f "$database" ]; then
	"$python_command" "$script_dir/iom_vendor.py" sync-db \
		"$sheet" \
		"$history" \
		"$database"
fi

"$python_command" "$script_dir/iom_vendor.py" normalize \
	"$repo_export" \
	"$sheet" \
	--database "$database"

"$python_command" "$script_dir/iom_vendor.py" refresh-nukatrader \
	"$sheet" \
	"$history" \
	"$database" \
	--max-age-days 30

"$python_command" "$script_dir/iom_vendor.py" select-unlocked \
	"$repo_export" \
	"$sheet"

"$python_command" "$script_dir/iom_vendor.py" build \
	"$sheet" \
	"$base_config" \
	"$priced_config"

"$python_command" -m json.tool "$priced_config" >/dev/null

if [ -f "$live_config" ]; then
	backup_config="$live_config.backup.$(date '+%Y%m%d-%H%M%S')"
	cp -p -- "$live_config" "$backup_config"
	printf 'Backed up live config to %s\n' "$backup_config"
fi

cp -- "$priced_config" "$live_config"

if ! cmp -s -- "$priced_config" "$live_config"; then
	printf 'error: installed config does not match generated config\n' >&2
	exit 1
fi

printf 'Installed and verified %s\n' "$live_config"

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "tools" / "iom_vendor.py"
SPEC = importlib.util.spec_from_file_location("iom_vendor", SCRIPT)
assert SPEC and SPEC.loader
iom_vendor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(iom_vendor)


class NukaTraderTests(unittest.TestCase):
    def test_select_unlocked_excludes_transfer_locked_items(self):
        with tempfile.TemporaryDirectory() as directory:
            dump_path = Path(directory) / "items.ini"
            csv_path = Path(directory) / "prices.csv"
            items = []
            for name, locked, learned in (
                ("Sell Me", False, False),
                ("Keep Me", True, False),
                ("Plan: Known", False, True),
                ("Plan: Unknown", False, False),
            ):
                items.append({
                    "text": name,
                    "count": 2,
                    "isTradable": True,
                    "isTransferLocked": locked,
                    "filterFlag": 64,
                    "itemLevel": 0,
                    "numLegendaryStars": 0,
                    "isLegendary": False,
                    "isLearnedRecipe": learned,
                })
            dump_path.write_text(json.dumps({
                "characterInventories": {
                    "Test": {"playerInventory": items, "stashInventory": []}
                }
            }))
            rows = []
            for name in ("Sell Me", "Keep Me", "Plan: Known", "Plan: Unknown"):
                row = {column: "" for column in iom_vendor.CSV_COLUMNS}
                row.update({
                    "Item Name": name,
                    "Price Lookup Name": name,
                    "Category": "NOTES" if name.startswith("Plan: ") else "AID",
                    "Available Quantity": "2",
                    "Suggested Price": "10",
                })
                rows.append(row)
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=iom_vendor.CSV_COLUMNS, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(rows)

            iom_vendor.select_unlocked(dump_path, csv_path)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                selected = {row["Item Name"]: row["Quantity to Sell"] for row in csv.DictReader(handle)}
            self.assertEqual(selected, {
                "Sell Me": "2",
                "Keep Me": "",
                "Plan: Known": "2",
                "Plan: Unknown": "",
            })

    def test_listed_price_uses_inventomatic_28_vending_data(self):
        item = {
            "isOffered": False,
            "offerValue": 0,
            "vendingData": {
                "machineType": 1,
                "price": 400,
                "isVendedOnOtherMachine": True,
            },
        }
        self.assertEqual(iom_vendor.listed_vendor_price(item), 400)

    def test_slug_matches_nukatrader_conventions(self):
        self.assertEqual(iom_vendor.nukatrader_slug("Recipe: S'mores"), "smores")
        self.assertEqual(
            iom_vendor.nukatrader_slug("Plan: Field Scribe's Hat"),
            "field-scribes-hat",
        )

    def test_parse_plan_price_and_modified_date(self):
        page = """
        <script>{"dateModified":"2022-08-31T17:28:11+00:00"}</script>
        <div>Price Low</div><div>1,000 caps</div>
        <div>Price High</div><div>2,000 caps</div>
        <div>Estimated Value</div><div>1,500 caps</div>
        """
        self.assertEqual(
            iom_vendor.parse_nukatrader_page(page),
            (1000, 2000, 1500, "2022-08-31"),
        )

    def test_refresh_keeps_other_source_as_active_price(self):
        sitemap = (
            "<urlset><url><loc>https://nukatrader.com/apparel/test-hat/"
            "</loc></url></urlset>"
        )
        page = """
        <script>{"dateModified":"2022-09-01T00:00:00+00:00"}</script>
        <div>Low: 25 caps</div><div>High: 100 caps</div>
        <div>Recommended: 63 caps</div>
        """

        def fake_fetch(url):
            return sitemap if url.endswith("sitemap.xml") else page

        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "prices.csv"
            history_path = Path(directory) / "history.csv"
            row = {column: "" for column in iom_vendor.CSV_COLUMNS}
            row.update({
                "Item Name": "Test Hat",
                "Price Lookup Name": "Test Hat",
                "Category": "APPAREL",
                "Suggested Price": "80",
                "Pricing Source": "Community baseline (2026)",
            })
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=iom_vendor.CSV_COLUMNS)
                writer.writeheader()
                writer.writerow(row)

            with (
                patch.object(iom_vendor, "NUKATRADER_SITEMAPS", ("https://test/sitemap.xml",)),
                patch.object(iom_vendor, "fetch_text", side_effect=fake_fetch),
            ):
                iom_vendor.refresh_nukatrader(csv_path, history_path, "2026-07-14")
                iom_vendor.refresh_nukatrader(csv_path, history_path, "2026-07-14")

            with csv_path.open(newline="", encoding="utf-8") as handle:
                refreshed = next(csv.DictReader(handle))
            self.assertEqual(refreshed["Suggested Price"], "80")
            self.assertEqual(refreshed["NukaTrader Recommended"], "63")
            self.assertEqual(refreshed["NukaTrader Source Modified"], "2022-09-01")

            with history_path.open(newline="", encoding="utf-8") as handle:
                history = list(csv.DictReader(handle))
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["Recommended Price"], "63")


if __name__ == "__main__":
    unittest.main()

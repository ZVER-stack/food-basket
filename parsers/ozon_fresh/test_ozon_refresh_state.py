"""Offline selective-refresh persistence and honest coverage output."""
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import ozon_fresh_fast as core
from ozon_refresh_state import Baseline, choose_samples, RefreshStore


def product(sku="1"):
    return dict(product_id=sku, name="Milk", price="100", price_regular="110",
                amount="1 л", parsed_at="2026-10-18 12:00:00", extra={"sample_only": True})


class RefreshStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.zones = [{"h3_index": str(i)} for i in range(197)]

    def baseline(self, missing=None, status="complete", fingerprint="fp"):
        path = self.root / "baseline.sqlite"
        state = core.State(path, fingerprint)
        for zone in self.zones:
            if zone["h3_index"] != missing:
                state.mark(zone["h3_index"], status, [product()])
        state.db.close()
        return path

    def test_rejects_incomplete_unexpected_or_fingerprint_changed_baseline(self):
        path = self.baseline(missing="196")
        with self.assertRaisesRegex(core.ParseError, "exactly all"):
            Baseline(path, self.zones, "fp")
        state = core.State(path, "fp"); state.mark("196", "error"); state.db.close()
        with self.assertRaisesRegex(core.ParseError, "incomplete"):
            Baseline(path, self.zones, "fp")
        with self.assertRaisesRegex(core.ParseError, "fingerprint"):
            Baseline(path, self.zones, "different")
        state = core.State(path, "fp"); state.mark("196", "complete", [product()]); state.mark("extra", "no_delivery"); state.db.close()
        with self.assertRaisesRegex(core.ParseError, "exactly all"):
            Baseline(path, self.zones, "fp")

    def test_house_normalization_gaps_no_delivery_and_baseline_file_unchanged(self):
        path = self.baseline(status="collected_with_gaps")
        state = core.State(path, "fp"); state.mark("1", "no_delivery"); state.db.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        baseline = Baseline(path, self.zones, "fp"); self.addCleanup(baseline.close)
        self.assertTrue(baseline.has_gaps)
        row = baseline.house("0")[0]
        self.assertEqual(list(row), core.COLUMNS)
        self.assertEqual((row["service"], row["h3_index"]), ("ozon_fresh", "0"))
        self.assertEqual(baseline.house("1"), [])
        self.assertEqual(baseline.skus('1'),set())
        self.assertEqual(baseline.skus('0'),{'1'})
        with self.assertRaises(sqlite3.OperationalError):
            baseline.db.execute("DELETE FROM zones")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_category_counts_exclude_empty_and_sold_out_tail_pages(self):
        path = self.baseline()
        state = core.State(path, "fp")
        key = json.dumps(["0", "cat"])
        for page, rows, sold in ((1, [product()], False), (12, [product()], False), (13, [], False), (99, [product()], True)):
            state.put_page(key, f"https://www.ozon.ru/category/c/?page={page}" + ("&sold_out_page=1" if sold else ""), "t", rows, None)
        state.put_page(json.dumps(["1", "cat"]), "https://www.ozon.ru/category/c/?page=200", "t", [product()], None)
        state.db.close()
        baseline = Baseline(path, self.zones, "fp"); self.addCleanup(baseline.close)
        self.assertEqual(baseline.category_pages("0"), {"cat": 12})

    def test_sampling_is_deterministic_unique_stratified_and_in_page_range(self):
        cats = [{"category_id": str(i), "scope": "food" if i < 7 else "household_chemicals",
                 "url": f"https://www.ozon.ru/category/c-{i}/?miniapp=supermarket&paginator_token=old&sort=old"} for i in range(10)]
        counts = {str(i): i + 1 for i in range(10)}
        result = choose_samples(cats, counts, "house", "seed")
        self.assertEqual(result, choose_samples(cats, counts, "house", "seed"))
        self.assertEqual(len({r["category"]["category_id"] for r in result}), 4)
        self.assertEqual([r["category"]["scope"] for r in result].count("food"), 2)
        for sample in result:
            self.assertTrue(1 <= sample["page"] <= counts[sample["category"]["category_id"]])
            self.assertEqual(set(parse_qs(urlsplit(sample["url"]).query)), {"miniapp", "page"})
        self.assertEqual(len(choose_samples(cats[:2], {}, "h", "s", pages=4)), 2)

    def test_exports_all_197_coverage_without_removing_unseen_baseline(self):
        baseline = Baseline(self.baseline(), self.zones, "fp"); self.addCleanup(baseline.close)
        store = RefreshStore(self.root / "refresh", baseline.identity, "fp", 7200); self.addCleanup(store.close)
        observed = product("new"); observed["extra"] = {"sample_only": True, "ordinary_price_verified_for_address": False,'ordinary_price_inferred':True}
        store.save_house("0", {"seen_skus": ["new"], "unchecked_baseline_skus": ["1"], "baseline_skus_count": 1},
                         {"new": observed}, [{"product_id": "changed", "reason": "needs price"}], "sample_checked")
        store.save_house("1", {}, {}, [], "partial")
        report = store.export(self.zones, baseline)
        self.assertFalse(report["complete"]); self.assertFalse(report["sampling_plan_completed"])
        self.assertEqual((report["houses_sample_checked"], report["houses_partial"], report["houses_not_checked"]), (1, 1, 195))
        self.assertEqual(report["unchecked_baseline_products"], 197)
        self.assertEqual(report["removed_confirmed"], 0)
        self.assertEqual(report['inferred_ordinary_prices'],1)
        self.assertEqual(report['new_observed_products'],1)
        coverage = [json.loads(line) for line in (store.output / "coverage.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(coverage), 197)
        self.assertEqual(coverage[-1]["status"], "not_checked")
        with (store.output / "refresh_observations.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream); rows = list(reader)
            self.assertEqual(reader.fieldnames, core.COLUMNS)
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(rows[0][key] for key in core.REQUIRED))
        self.assertEqual(json.loads(rows[0]["extra"])["ordinary_price_verified_for_address"], False)
        with self.assertRaises(core.ParseError):
            store.save_house("0", {}, {"bad": dict(product("bad"), amount="")}, [], "sample_checked")

    def test_resume_keeps_start_and_guards_inputs_budget(self):
        folder = self.root / "refresh"
        with patch("ozon_refresh_state.time.time", return_value=1000):
            store = RefreshStore(folder, "base", "fp", 7200)
        store.save_house("0", {}, {}, [], "sample_checked")
        store.close()
        with patch("ozon_refresh_state.time.time", return_value=9999):
            resumed = RefreshStore(folder, "base", "fp", 7200)
        self.assertEqual(resumed.started_unix, 1000)
        self.assertTrue(resumed.done("0")); self.assertFalse(resumed.done("1")); resumed.close()
        for baseline, fingerprint, seconds in (("other", "fp", 7200), ("base", "other", 7200), ("base", "fp", 8000)):
            with self.assertRaisesRegex(core.ParseError, "cannot change"):
                RefreshStore(folder, baseline, fingerprint, seconds)

    def test_unconfirmed_partial_observations_remain_staged_until_address_barrier(self):
        baseline = Baseline(self.baseline(), self.zones, "fp"); self.addCleanup(baseline.close)
        store = RefreshStore(self.root / "refresh", baseline.identity, "fp", 7200); self.addCleanup(store.close)
        store.save_house("0", {"address_final_confirmed": False}, {"1": product()}, [], "partial")
        report = store.export(self.zones, baseline)
        self.assertEqual(report["observations_rows"], 0)
        self.assertEqual(report["unchecked_baseline_products"], 197)
        self.assertIn("1", store.get_house("0")["observations"])
        coverage = json.loads((store.output / "coverage.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(coverage["staged_observations_rows"], 1)
        self.assertFalse(coverage["address_final_confirmed"])
        store.save_house("0", {"address_final_confirmed": True}, {"1": product()}, [], "partial")
        report = store.export(self.zones, baseline)
        self.assertEqual(report["observations_rows"], 1)
        self.assertEqual(report["unchecked_baseline_products"], 196)


if __name__ == "__main__":
    unittest.main()

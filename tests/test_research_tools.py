"""Synthetic fixtures only. These tests provide no evidence of market performance."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.research_tools import journal, main, study


def sample():
    return {
        "metadata": {
            "source": "synthetic unit test", "retrieved_at": "2026-01-10T00:00:00Z",
            "label": "SYNTHETIC", "symbol": "TEST", "interval": "day",
            "session": "regular", "adjustment": "none", "sample": "development",
            "coverage_notes": "Invented observations, not an exchange calendar",
        },
        "bars": [
            {"start": f"2026-01-0{i}T14:30:00Z", "end": f"2026-01-0{i}T21:00:00Z",
             "open": opening, "close": close, "signal": True}
            for i, (opening, close) in enumerate([(90, 100), (110, 121), (120, 132), (130, 143)], 1)
        ],
    }


def episode(identifier, pnl, day, kind="live", risk=100):
    return {"id": identifier, "kind": kind, "status": "closed",
            "closed_at": f"2026-01-0{day}T21:00:00Z", "pnl_net": pnl,
            "initial_risk": risk, "setup": "synthetic", "process": "good"}


class EventStudyTests(unittest.TestCase):
    def test_next_open_not_signal_close_and_both_costs(self):
        result = study(sample(), 1, 10)
        first = result["events"][0]
        self.assertEqual(first["entry_price"], 110)
        self.assertAlmostEqual(first["gross_return"], 0.1)
        self.assertAlmostEqual(first["net_return"], 0.098)
        self.assertEqual(result["selected"]["n"], 3)
        self.assertEqual(result["skipped_incomplete_windows"], 1)

    def test_short_costs_reduce_return(self):
        self.assertAlmostEqual(study(sample(), 1, 10, "short")["events"][0]["net_return"], -0.102)

    def test_horizon_overlap_and_tail(self):
        result = study(sample(), 2, 0)
        self.assertEqual(result["selected"]["n"], 1)
        self.assertEqual(result["events"][0]["exit_price"], 132)
        self.assertEqual(result["skipped_overlapping_windows"], 1)
        self.assertEqual(result["skipped_incomplete_windows"], 2)
        self.assertEqual(study(sample(), 2, 0, allow_overlap=True)["selected"]["n"], 2)

    def test_no_events_is_not_zero_expectancy(self):
        data = sample()
        for bar in data["bars"]:
            bar["signal"] = False
        result = study(data, 1, 0)
        self.assertIsNone(result["selected"]["mean"])
        self.assertEqual(result["unconditional_overlapping_baseline"]["n"], 3)

    def test_invalid_bars(self):
        alterations = [
            ("open", 0), ("open", True), ("close", float("nan")),
            ("signal", "false"), ("interpolated", True),
            ("start", "2026-01-01T14:30:00"),
            ("end", "2026-02-01T21:00:00Z"),
        ]
        for key, value in alterations:
            with self.subTest(key=key, value=value):
                data = sample()
                data["bars"][0][key] = value
                with self.assertRaises(ValueError):
                    study(data, 1, 0)

    def test_rejects_reordering_and_overlapping_bars(self):
        data = sample()
        data["bars"][0], data["bars"][1] = data["bars"][1], data["bars"][0]
        with self.assertRaises(ValueError):
            study(data, 1, 0)
        data = sample()
        data["bars"][1]["start"] = "2026-01-01T20:00:00Z"
        with self.assertRaises(ValueError):
            study(data, 1, 0)

    def test_rejects_invalid_parameters_and_provenance(self):
        for horizon, costs in [(0, 0), (True, 0), (1, -1), (1, float("inf"))]:
            with self.assertRaises(ValueError):
                study(sample(), horizon, costs)
        data = sample()
        del data["metadata"]["source"]
        with self.assertRaises(ValueError):
            study(data, 1, 0)

    def test_does_not_mutate_input(self):
        data = sample()
        original = deepcopy(data)
        study(data, 1, 0)
        self.assertEqual(data, original)


class JournalTests(unittest.TestCase):
    def test_metrics_do_not_pool_paper_open_or_passed(self):
        rows = [episode("a", 100, 1), episode("b", -50, 2), episode("c", 0, 3),
                episode("paper", 9999, 4, kind="paper"),
                {"id": "open", "kind": "live", "status": "open"},
                {"id": "pass", "kind": "passed", "status": "passed"}]
        result = journal({"records": rows}, group_by="setup")
        s = result["summary"]
        self.assertEqual(s["closed_episodes"], 3)
        self.assertEqual(s["net_pnl"], 50)
        self.assertAlmostEqual(s["win_rate"], 1 / 3)
        self.assertAlmostEqual(s["sample_expectancy_net"], 50 / 3)
        self.assertEqual(s["profit_factor"], 2)
        self.assertAlmostEqual(s["mean_R"], 1 / 6)
        self.assertEqual(s["max_closed_episode_drawdown_currency"], 50)
        self.assertEqual(s["process_outcomes"]["good_process_loss"], 1)
        self.assertEqual(result["groups"]["synthetic"]["closed_episodes"], 3)
        self.assertEqual(journal({"records": rows}, kind="paper")["summary"]["net_pnl"], 9999)

    def test_empty_no_losses_and_missing_r(self):
        empty = journal({"records": []})["summary"]
        self.assertIsNone(empty["sample_expectancy_net"])
        self.assertIsNone(empty["max_closed_episode_drawdown_currency"])
        result = journal({"records": [episode("a", 5, 1, risk=None)]})["summary"]
        self.assertIsNone(result["profit_factor"])
        self.assertIsNone(result["mean_R"])
        self.assertEqual(result["R_missing"], 1)

    def test_simultaneous_closes_and_chronological_sorting(self):
        rows = [episode("a", -50, 2), episode("b", -100, 1), episode("c", 100, 1)]
        result = journal({"records": rows})["summary"]
        self.assertEqual(result["max_closed_episode_drawdown_currency"], 50)

    def test_rejects_duplicates_invalid_risk_and_missing_costed_pnl(self):
        row = episode("a", 5, 1)
        with self.assertRaises(ValueError):
            journal({"records": [row, row]})
        for key, value in [("initial_risk", 0), ("pnl_net", None), ("process", "great"),
                           ("status", "passed"), ("closed_at", "2026-01-01")]:
            with self.subTest(key=key):
                changed = dict(row, **{key: value})
                with self.assertRaises(ValueError):
                    journal({"records": [changed]})


class CommandTests(unittest.TestCase):
    def run_input(self, raw, *args):
        # Temporary synthetic input is isolated from research and journal records.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic-input.json"
            path.write_text(raw, encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([args[0], str(path), *args[1:]])
            return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_study_and_hashes(self):
        code, out, err = self.run_input(json.dumps(sample()), "study", "--holding-bars", "1", "--cost-bps", "10")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(len(result["reproducibility"]["input_sha256"]), 64)
        self.assertEqual(len(result["reproducibility"]["code_sha256"]), 64)

    def test_cli_journal_empty(self):
        code, out, err = self.run_input('{"records": []}', "journal")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["summary"]["closed_episodes"], 0)

    def test_invalid_json_is_an_error_not_an_empty_result(self):
        for raw in ('{"records": NaN}', '{"records": [], "records": []}', '[1]', '{'):
            with self.subTest(raw=raw):
                code, out, err = self.run_input(raw, "journal")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()

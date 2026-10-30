"""Local, explicit-input research calculations. No networking or brokerage code."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
import sys


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamps must be strings with an explicit timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamps require an explicit timezone")
    return result


def number(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} must be finite" + (" and positive" if positive else ""))
    return value


def text_field(obj, name):
    value = obj.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def distribution(values):
    return {
        "n": len(values),
        "mean": mean(values) if values else None,
        "median": median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "win_fraction": sum(v > 0 for v in values) / len(values) if values else None,
    }


def study(payload, holding_bars, cost_bps, side="long", allow_overlap=False):
    if type(holding_bars) is not int or holding_bars < 1:
        raise ValueError("holding_bars must be a positive integer")
    number(cost_bps, "cost_bps")
    if cost_bps < 0 or side not in ("long", "short"):
        raise ValueError("cost_bps must be nonnegative and side long or short")
    if type(allow_overlap) is not bool:
        raise ValueError("allow_overlap must be boolean")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    for key in ("source", "retrieved_at", "label", "symbol", "interval", "session",
                "adjustment", "sample", "coverage_notes"):
        text_field(metadata, key)
    if metadata["label"] not in ("HISTORICAL", "SYNTHETIC"):
        raise ValueError("completed study inputs must be HISTORICAL or SYNTHETIC")
    if metadata["sample"] not in ("development", "validation", "out_of_sample"):
        raise ValueError("sample must be development, validation, or out_of_sample")
    retrieved = timestamp(metadata["retrieved_at"])
    bars = payload.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("bars must be a nonempty list")
    previous_end = None
    for bar in bars:
        if not isinstance(bar, dict):
            raise ValueError("each bar must be an object")
        start, end = timestamp(bar.get("start")), timestamp(bar.get("end"))
        if start >= end or (previous_end is not None and start < previous_end):
            raise ValueError("bars must be strictly ordered and non-overlapping")
        if end > retrieved:
            raise ValueError("bar is incomplete at retrieval time")
        if type(bar.get("interpolated", False)) is not bool or bar.get("interpolated", False):
            raise ValueError("interpolated bars cannot be treated as observations")
        number(bar.get("open"), "open", positive=True)
        number(bar.get("close"), "close", positive=True)
        if type(bar.get("signal")) is not bool:
            raise ValueError("each bar requires a boolean signal")
        previous_end = end

    direction = 1 if side == "long" else -1
    costs = 2 * cost_bps / 10000
    events, baseline = [], []
    incomplete = overlaps = 0
    last_exit = -1
    for i, bar in enumerate(bars):
        entry, exit_index = i + 1, i + holding_bars
        if exit_index >= len(bars):
            incomplete += int(bar["signal"])
            continue
        price_in, price_out = bars[entry]["open"], bars[exit_index]["close"]
        gross = direction * (price_out / price_in - 1)
        net = gross - costs
        baseline.append(net)
        if not bar["signal"]:
            continue
        if not allow_overlap and entry <= last_exit:
            overlaps += 1
            continue
        events.append({
            "signal_at": bar["end"], "entry_at": bars[entry]["start"],
            "exit_at": bars[exit_index]["end"], "entry_price": price_in,
            "exit_price": price_out, "gross_return": gross, "net_return": net,
        })
        last_exit = exit_index
    return {
        "analysis": "conditional_forward_returns_not_portfolio_backtest",
        "metadata": metadata,
        "parameters": {"holding_bars": holding_bars, "cost_bps_per_side": cost_bps,
                       "side": side, "allow_overlap": allow_overlap},
        "signals": sum(bar["signal"] for bar in bars),
        "skipped_incomplete_windows": incomplete, "skipped_overlapping_windows": overlaps,
        "selected": distribution([event["net_return"] for event in events]),
        "unconditional_overlapping_baseline": distribution(baseline),
        "events": events,
        "limitations": [
            "Signal point-in-time integrity and holdout status are researcher supplied.",
            "No calendar, missing-session, liquidity, or universe-survivorship validation.",
            "Baseline windows may overlap; observations are not assumed independent.",
            "Simple underlying returns, not options P&L or compounded portfolio returns.",
            "Costs are per-side basis points of entry notional; financing/dividends excluded.",
        ],
    }


def closed_metrics(records):
    pnls = [r["pnl_net"] for r in records]
    winners, losers = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    rs = [r["pnl_net"] / r["initial_risk"] for r in records if r.get("initial_risk") is not None]
    # Aggregate simultaneous closes so row order cannot invent a drawdown.
    by_time = defaultdict(float)
    process = Counter()
    for row in records:
        by_time[timestamp(row["closed_at"])] += row["pnl_net"]
        outcome = "win" if row["pnl_net"] > 0 else "loss" if row["pnl_net"] < 0 else "flat"
        process[f"{row['process']}_process_{outcome}"] += 1
    cumulative = peak = drawdown = 0
    for time in sorted(by_time):
        cumulative += by_time[time]
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return {
        "closed_episodes": len(records), "winners": len(winners), "losers": len(losers),
        "break_even": sum(p == 0 for p in pnls), "net_pnl": sum(pnls),
        "win_rate": len(winners) / len(records) if records else None,
        "average_winner": mean(winners) if winners else None,
        "average_loser": mean(losers) if losers else None,
        "sample_expectancy_net": mean(pnls) if pnls else None,
        "profit_factor": sum(winners) / abs(sum(losers)) if losers else None,
        "profit_factor_note": None if losers else "undefined: no losing episodes",
        "mean_R": mean(rs) if rs else None, "R_count": len(rs),
        "R_missing": len(records) - len(rs),
        "max_closed_episode_drawdown_currency": drawdown if records else None,
        "process_outcomes": dict(sorted(process.items())),
    }


def journal(payload, kind="live", group_by=None):
    if kind not in ("live", "paper") or group_by not in (None, "setup", "regime", "horizon"):
        raise ValueError("invalid journal kind or grouping")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    seen = set()
    counts = Counter()
    selected = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("each journal record must be an object")
        record_id = text_field(row, "id")
        if record_id in seen:
            raise ValueError(f"duplicate journal id: {record_id}")
        seen.add(record_id)
        if row.get("kind") not in ("live", "paper", "passed"):
            raise ValueError("kind must be live, paper, or passed")
        if row.get("status") not in ("open", "closed", "passed"):
            raise ValueError("status must be open, closed, or passed")
        if (row["kind"] == "passed") != (row["status"] == "passed"):
            raise ValueError("passed kind and status must agree")
        counts[f"{row['kind']}_{row['status']}"] += 1
        for field in ("setup", "regime", "horizon"):
            if field in row:
                text_field(row, field)
        if row["status"] != "closed":
            continue
        timestamp(row.get("closed_at"))
        number(row.get("pnl_net"), "pnl_net")
        if row.get("process") not in ("good", "bad", "unrated"):
            raise ValueError("closed process must be good, bad, or unrated")
        if row.get("initial_risk") is not None:
            number(row["initial_risk"], "initial_risk", positive=True)
        if row["kind"] == kind:
            selected.append(row)
    result = {
        "analysis": "closed_trade_episodes_not_account_equity_performance",
        "kind": kind, "all_record_counts": dict(sorted(counts.items())),
        "summary": closed_metrics(selected),
        "limitations": [
            "One currency per input; P&L must already include all costs.",
            "No fill reconciliation or marked-to-market/cash-flow-adjusted account returns.",
            "Sample metrics are descriptive, not evidence of a durable edge.",
        ],
    }
    if group_by:
        groups = defaultdict(list)
        for row in selected:
            groups[row.get(group_by, "UNSPECIFIED")].append(row)
        result["group_by"] = group_by
        result["groups"] = {key: closed_metrics(rows) for key, rows in sorted(groups.items())}
    return result


def reject_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    event = commands.add_parser("study", help="underlying conditional forward-return study")
    event.add_argument("input", type=Path)
    event.add_argument("--holding-bars", type=int, required=True)
    event.add_argument("--cost-bps", type=float, required=True, help="per side, entry notional")
    event.add_argument("--side", choices=("long", "short"), default="long")
    event.add_argument("--allow-overlap", action="store_true")
    log = commands.add_parser("journal", help="closed episode performance, one kind at a time")
    log.add_argument("input", type=Path)
    log.add_argument("--kind", choices=("live", "paper"), default="live")
    log.add_argument("--group-by", choices=("setup", "regime", "horizon"))
    args = parser.parse_args(argv)
    try:
        raw = args.input.read_bytes()
        payload = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        if args.command == "study":
            result = study(payload, args.holding_bars, args.cost_bps, args.side, args.allow_overlap)
        else:
            result = journal(payload, args.kind, args.group_by)
        result["reproducibility"] = {
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

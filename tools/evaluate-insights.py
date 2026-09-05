"""Reproduce the review's explicitly scoped historical proxies, without changing assets.

py -B -X utf8 tools\\evaluate-insights.py --runtime --node node \
    --output tests\\fixtures\\insights-evaluation.json
Only --output writes a file, beneath this checkout. No legacy checkout or dependency is used.
"""
import argparse
import collections
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_builder():
    path = ROOT / "tools" / "build-insights.py"
    spec = importlib.util.spec_from_file_location("insights_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture_build(builder):
    """Capture the production predicates, rather than inventing a second definition."""
    values = {}
    previous = sys.getprofile()

    def profile(frame, event, _arg):
        if event == "return" and frame.f_code is builder.build.__code__:
            values.update(frame.f_locals)

    try:
        sys.setprofile(profile)
        insights = builder.build()
    finally:
        sys.setprofile(previous)
    return insights, values


def cases_from(builder, values):
    return [{
        "date": date, "shift": shift,
        "stores": {sid: sorted(builder.shown(m) for m in names)
                   for sid, names in stores.items()},
        "listed": sorted(builder.shown(m) for m in values["listed_on"](date, stores)),
        "trainees": sorted({builder.shown(m) for names in stores.values() for m in names
                            if values["is_trainee"](builder.shown(m), date)}),
    } for (date, shift), stores in sorted(values["cell"].items())]


def thresholds(cases):
    result = {}
    for table in ([5, 12], [5, 12, 15]):
        hits = four = correct_four = wins = losses = 0
        for case in cases:
            n, truth = len(case["listed"]), len(case["stores"])
            baseline = 1 + (n > 5) + (n > 12)
            prediction = 1 + sum(n > t for t in table)
            hits += prediction == truth
            four += prediction == 4
            correct_four += prediction == 4 and truth == 4
            wins += prediction == truth and baseline != truth
            losses += prediction != truth and baseline == truth
        result[str(table)] = {
            "n": len(cases), "hits": hits, "predictedFour": four,
            "correctFour": correct_four, "falseFour": four - correct_four,
            "wins": wins, "losses": losses,
        }
    return result


def legacy_move_comparison(builder, cell):
    """120-day, oracle-open, independent argmax control; NOT the shipping assignment."""
    days = sorted({d for d, _ in cell})
    both = [d for d in days if (d, "昼") in cell and (d, "夜") in cell]
    start = both[len(both) // 2]
    result = {scope: dict(n=0, tendency=0, smoothed=0, zeroFallback=0)
              for scope in ("twoOrMore", "allNights")}
    for date in both:
        if date < start:
            continue
        lo = (dt.date.fromisoformat(date) - dt.timedelta(days=120)).isoformat()
        chance, at = collections.Counter(), collections.Counter()
        personal = collections.defaultdict(collections.Counter)
        overall = collections.defaultdict(collections.Counter)
        for past in days:
            if not lo <= past < date:
                continue
            for shift in builder.SHIFTS:
                stores = cell.get((past, shift), {})
                for name in {m for names in stores.values() for m in names}:
                    for sid in stores:
                        chance[name, shift, sid] += 1
                for sid, names in stores.items():
                    for name in names:
                        at[name, shift, sid] += 1
            lunch = {m: s for s, names in cell.get((past, "昼"), {}).items() for m in names}
            night = {m: s for s, names in cell.get((past, "夜"), {}).items() for m in names}
            for name in lunch.keys() & night.keys():
                personal[name, lunch[name]][night[name]] += 1
                overall[lunch[name]][night[name]] += 1
        lunch = {m: s for s, names in cell[date, "昼"].items() for m in names}
        open_ids = [s for s in builder.IDS if s in cell[date, "夜"]]
        for sid, names in cell[date, "夜"].items():
            for name in names:
                if name not in lunch or sum(chance[name, "夜", s] for s in open_ids) < 5:
                    continue
                tendency = {s: (at[name, "夜", s] + .5) / (chance[name, "夜", s] + 2)
                            if chance[name, "夜", s] else 0 for s in open_ids}
                counts = personal[name, lunch[name]]
                if sum(counts.values()) < 3:
                    counts = overall[lunch[name]]
                total = sum(counts.values())
                base = max(open_ids, key=tendency.get)
                smooth = max(open_ids, key=lambda s:
                             tendency[s] * ((counts[s] + .5) / (total + 2) if total else .25))
                zero = {s: tendency[s] * round(counts[s] / total, 3) if total else 0
                        for s in open_ids}
                fallback = max(open_ids, key=zero.get) if any(zero.values()) else base
                scopes = ["allNights"] + (["twoOrMore"] if len(open_ids) >= 2 else [])
                for scope in scopes:
                    row = result[scope]
                    row["n"] += 1
                    row["tendency"] += base == sid
                    row["smoothed"] += smooth == sid
                    row["zeroFallback"] += fallback == sid
    return {"from": start, "trainingDays": 120, "oracleOpenStores": True,
            "assignment": "independent_argmax", **result}


def observed_rows(rows, date, shift, shift_labels):
    return [r for r in rows if r["date"] < date or
            (r["date"] == date and shift == "夜" and
             shift_labels.get(r["shift"], r["shift"]) == "昼")]


def snapshots(builder, cases):
    original_load = builder.load_csv
    original_accuracy = builder.maid_accuracy
    original_calibration = builder.measure_calibration
    tables = {name: original_load(name, optional=True)
              for name in ("shifts.csv", "openings.csv")}
    # Neither of these display-only measurements is read by the forecast pipeline.
    builder.maid_accuracy = lambda *a, **k: {}
    builder.measure_calibration = lambda *a, **k: {}
    keep = ("stores", "shifts", "baseOpenRate", "typicalHeadcount", "weekdayOpenRate",
            "shiftSplitGivenOpen", "openCountPerShift", "openCountByHeadcount",
            "secondStoreByHome", "homeStaffShare", "rotation", "accuracy",
            "maidTendency", "traineeOutlook", "actual", "actualRoster", "actualWithoutRoster",
            "sampleWindow", "tendencyWindow")
    try:
        for case in cases:
            date, shift = case["date"], case["shift"]
            if date < "2026-02-14":
                continue

            def load(name, optional=False):
                if name in tables:
                    return observed_rows(tables[name], date, shift, builder.SHIFT_LABEL)
                return original_load(name, optional)

            builder.load_csv = load
            ins = builder.build()
            for key in ("actual", "actualRoster", "actualWithoutRoster"):
                assert shift not in ins[key].get(date, {}), (key, date, shift)
                assert all(day <= date for day in ins[key]), (key, date)
                previous = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
                ins[key] = {day: value for day, value in ins[key].items()
                            if day in (date, previous)}
            yield {"case": case, "ins": {key: ins[key] for key in keep}}
    finally:
        builder.load_csv = original_load
        builder.maid_accuracy = original_accuracy
        builder.measure_calibration = original_calibration


def paired_statistics(runtime):
    for row in runtime.values():
        if not isinstance(row, dict) or "wins" not in row:
            continue
        n = row["wins"] + row["losses"]
        row["exactPairedP"] = min(1, 2 * sum(math.comb(n, i)
            for i in range(min(row["wins"], row["losses"]) + 1)) / 2 ** n) if n else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="store_true", help="Rebuild every causal snapshot")
    parser.add_argument("--node", default=os.environ.get("NODE", "node"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        output = (ROOT / args.output).resolve()
        if not output.is_relative_to(ROOT):
            parser.error("--output must stay in this checkout")
    sources = ["data/schedule.js", "data/store-insights.js", "tools/build-insights.py", "app.js",
               "tools/evaluate-insights.py", "tools/evaluate-insights-runtime.js"]
    sources += [str(p.relative_to(ROOT)).replace("\\", "/")
                for p in sorted((ROOT / "tools" / "data").glob("*.csv"))]
    def digests():
        result = {}
        for name in sources:
            raw = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
            if name == "data/store-insights.js":
                raw = re.sub(rb'^\s*"generatedAt":.*$', b'', raw, flags=re.M)
            result[name] = hashlib.sha256(raw).hexdigest()
        return result

    input_digests = digests()
    builder = load_builder()
    ins, values = capture_build(builder)
    cases = cases_from(builder, values)
    report = {
        "inputSha256": input_digests,
        "hashNormalization": "CRLF to LF; generatedAt line excluded from shipped insights",
        "conditions": {
            "asOf": ins["historyRange"]["to"],
            "pool": "unique listed_on/promotion_dates, including kitchen",
            "metadata": "current roster/home/accounts held fixed",
            "input": "historical attendance proxy, not actual advance schedules",
            "holdout": "previously explored data; no independent future holdout",
            "snapshot": "shifts AND openings before target; lunch included for night only",
        },
        "thresholds": thresholds(cases),
        "rosterCoverage": ins["rosterCoverage"],
        "traineeCoverage": ins["traineeCoverage"],
        "sameDayMaidMoveSummary": ins["sameDayMaidMoveSummary"],
        "legacyMove": legacy_move_comparison(builder, values["cell"]),
    }
    if args.runtime:
        payload = {"snapshots": list(snapshots(builder, cases))}
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        result = subprocess.run(
            [args.node, str(ROOT / "tools" / "evaluate-insights-runtime.js")],
            input=json.dumps(payload, ensure_ascii=False), encoding="utf-8",
            capture_output=True, cwd=ROOT, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), startupinfo=startup)
        report["runtime"] = json.loads(result.stdout)
        paired_statistics(report["runtime"])
    if digests() != report["inputSha256"]:
        raise RuntimeError("Inputs changed during evaluation; rerun on a stable checkout")
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()

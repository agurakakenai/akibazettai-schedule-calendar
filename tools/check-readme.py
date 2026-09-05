"""Compare scoped README claims with shipped data; this is not a factual audit.

py -B -X utf8 tools\\check-readme.py
Historical sections are excluded explicitly. Missing, duplicate or malformed target
sections fail closed; a correct number in an unrelated section cannot rescue a claim.
"""
from decimal import Decimal
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_insights():
    raw = (ROOT / "data" / "store-insights.js").read_text(encoding="utf-8")
    return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])


def load_readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def without_fences(text):
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        match = re.match(r"[ \t]*(`{3,}|~{3,})", line)
        masked = fence is not None or match is not None
        if match:
            marker = match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        lines.append(re.sub(r"[^\r\n]", " ", line) if masked else line)
    return "".join(lines)


def headings(text):
    return list(re.finditer(r"^(#{1,6})[ \t]+(.+?)[ \t\r]*$", without_fences(text), re.M))


def section_end(matches, index, text):
    level = len(matches[index][1])
    return next((m.start() for m in matches[index + 1:] if len(m[1]) <= level), len(text))


def section(text, title):
    matches = headings(text)
    indexes = [i for i, m in enumerate(matches) if m[2] == title]
    if len(indexes) != 1:
        raise ValueError(f"見出し「{title}」は1つ必要です（{len(indexes)}件）")
    index = indexes[0]
    return text[matches[index].end():section_end(matches, index, text)]


def current_prose(text):
    matches = headings(text)
    spans = []
    for index, match in enumerate(matches):
        if match[2].startswith("過去測定") or match[2] in (
                "README の数字が、出荷している値と合っているか",
                "この節を書き換えたときのこと"):
            spans.append((match.start(), section_end(matches, index, text)))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + text[end:]
    return without_fences(text)


def build_claims(data):
    """One claim extracts all matching semantic occurrences in its own section."""
    claims = []

    def add(title, label, pattern, expected):
        claims.append((title, label, pattern, tuple(expected)))

    counts = data["openCountPerShift"]["昼"]
    add("開く店の数が表の上限に当たったら、そう断ります", "昼の件数と3店超",
        r"昼(\d+)件の記録に(\d+)件",
        (sum(counts.values()), sum(n for k, n in counts.items() if int(k) > 3)))
    move = data["sameDayMaidMoveSummary"]
    move_percent = round(100 * move["movedPersonPairs"] / move["personPairs"], 1) if move["personPairs"] else 0
    add("昼の記録から、夜の行き先を読み直します", "通し勤務の人回・移動人回・移動率",
        r"昼夜とも出た(\d+)人回のうち(\d+)人回（(\d+(?:\.\d+)?)%）が別の店",
        (move["personPairs"], move["movedPersonPairs"], move_percent))
    cov = data["rosterCoverage"]
    add("未掲載者の集計単位", "未掲載の期間",
        r"直近90日（(\d{4}-\d{2}-\d{2})〜(\d{4}-\d{2}-\d{2})）",
        (cov["from"], cov["to"]))
    number = r"(\d+(?:\.\d+)?)"
    for name, key, unit in (("全店ユニーク", "overall", "date-shift"),
                            ("店舗延べ", "storeSlots", "date-shift-store")):
        row = cov[key]
        pattern = r"\|\s*" + name + r"\s*\|\s*" + unit + r"\s*\|\s*"
        pattern += r"\s*\|\s*".join([number] * 5) + r"%\s*\|"
        add("未掲載者の集計単位", name, pattern, (
            row["cells"], row["unlistedPersonAppearances"], row["unlistedPerCell"],
            row["cellsWithUnlisted"], round(row["cellsWithUnlistedRate"] * 100, 1)))
    for sid in ("s1", "s2", "s3", "s4"):
        row = cov["byStore"][sid]
        pattern = r"\|\s*" + sid[1:] + r"号店\s*\|\s*"
        pattern += r"\s*\|\s*".join([number] * 5) + r"%\s*\|"
        add("店舗ごとの未掲載率", sid + "未掲載", pattern, (
            row["cells"], row["unlistedPersonAppearances"], row["unlistedPerCell"],
            row["cellsWithUnlisted"], round(row["cellsWithUnlistedRate"] * 100, 1)))
        row = data["traineeCoverage"]["byStore"][sid]
        pattern = r"\|\s*" + sid[1:] + r"号店\s*\|\s*"
        pattern += r"\s*\|\s*".join([number] * 5) + r"%\s*\|"
        add("店舗枠の見習い率", sid + "見習い", pattern, (
            row["cells"], row["traineePersonAppearances"], row["traineesPerCell"],
            row["cellsWithTrainees"], round(row["cellsWithTraineesRate"] * 100, 1)))
    add(None, "現在の在籍人数",
        r"(?:在籍|roster\s*(?:は)?)\s*(\d+)\s*名", (data["schedulePending"]["rostered"],))
    return claims


def equal_value(actual, expected):
    if isinstance(expected, str):
        return actual == expected
    return Decimal(actual) == Decimal(str(expected))


def check(text, data):
    errors = []
    # Remove historical subtrees before resolving current section headings.
    # Otherwise a historical heading can shadow or supply a current claim.
    current = current_prose(text)
    for title, label, pattern, expected in build_claims(data):
        try:
            scope = section(current, title) if title else current
        except ValueError as exc:
            errors.append(str(exc))
            continue
        scope = scope.replace("`", "").replace("**", "")
        matches = list(re.finditer(pattern, scope))
        if not matches:
            errors.append(f"{label}: 対象の主張が見つかりません")
        for match in matches:
            if len(match.groups()) != len(expected) or not all(
                    equal_value(a, e) for a, e in zip(match.groups(), expected)):
                errors.append(f"{label}: {match.groups()} != {expected}")
    return errors


def main():
    data = load_insights()
    errors = check(load_readme(), data)
    for error in errors:
        print("不一致: " + error)
    if errors:
        print(f"{len(errors)}件の不一致。出荷値・対象節・集計単位を確認してください。")
        return 1
    print(f"対象{len(build_claims(data))}主張群は出荷値と一致しました（全文の事実検証ではありません）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

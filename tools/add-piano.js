/*
 * ぴあのの9月前半のお給仕予定を data/schedule.js に追加する。
 *
 * 出典: 本人が公開した「9月前半ぉ給仕予定」の画像
 *
 *   2日  よるにゃん  -> 夜
 *   4日  よるにゃん  -> 夜
 *   6日  おーらす    -> 昼・夜（通し）
 *   7日  ひるにゃん  -> 昼
 *   8日  よるにゃん  -> 夜
 *   10日 よるにゃん  -> 夜
 *   12日 よるにゃん  -> 夜
 *   13日 よるにゃん  -> 夜
 *
 * 「おーらす」は閉店まで通しの意味。公式の昼にゃんこ投稿にも
 * `おすず(オーラス)` と `ぱん(-18)` が並んで出てくる（2019-12-04）ので、
 * 昼の枠に「オーラス」と書けば夜まで居るということ。よって6日は昼と夜の両方に入れる。
 *
 * ぴあのは公式サイトのメイドさん紹介にまだ載っていないが、予定を提出しているので
 * ノーマル以上。roster には入れ、homeStore（公式の配属）は分からないので空にする。
 */
const fs = require("fs");
const path = require("path");

const NAME = "ぴあの";
const AFTER = "いと";      // roster のどこに入れるか（キッチンにゃんこの手前）
const FILE = path.join(__dirname, "..", "data", "schedule.js");
const PLAN = {
  "2026-09-02": ["夜"],
  "2026-09-04": ["夜"],
  "2026-09-06": ["昼", "夜"],
  "2026-09-07": ["昼"],
  "2026-09-08": ["夜"],
  "2026-09-10": ["夜"],
  "2026-09-12": ["夜"],
  "2026-09-13": ["夜"]
};

let text = fs.readFileSync(FILE, "utf8");

// 1. roster に足す
const rosterBlock = /(roster:\s*\[)([\s\S]*?)(\n  \])/.exec(text);
if (!rosterBlock) {
  throw new Error("roster を読み取れませんでした");
}
let roster = [...rosterBlock[2].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
if (!roster.includes(NAME)) {
  const at = roster.indexOf(AFTER);
  if (at === -1) {
    throw new Error(`${AFTER} が roster にいません`);
  }
  roster.splice(at + 1, 0, NAME);
  const rebuilt =
    rosterBlock[1] +
    "\n    " +
    roster.map((n) => `"${n}"`).join(",\n    ") +
    rosterBlock[3];
  text = text.replace(rosterBlock[0], rebuilt);
  console.log(`roster に ${NAME} を追加（${AFTER} の次、${roster.length} 名）`);
} else {
  console.log(`roster には ${NAME} が既にいます`);
}

const rank = new Map(roster.map((n, i) => [n, i]));

// 2. 各シフトに足す（roster 順を保つ）
let added = 0;
let already = 0;
for (const [date, shifts] of Object.entries(PLAN)) {
  for (const shift of shifts) {
    const dayRe = new RegExp(`("${date}":\\s*\\{)([\\s\\S]*?)(\\n    \\})`);
    const day = dayRe.exec(text);
    if (!day) {
      throw new Error(`${date} が schedule にありません`);
    }
    const shiftRe = new RegExp(`("${shift}":\\s*\\[)([\\s\\S]*?)(\\n      \\])`);
    const block = shiftRe.exec(day[2]);
    if (!block) {
      throw new Error(`${date} の ${shift} がありません`);
    }

    const entries = [...block[2].matchAll(/\{[^}]*\}/g)].map((m) => m[0].trim());
    if (entries.some((e) => e.includes(`"${NAME}"`))) {
      already += 1;
      continue;
    }

    const withMine = [...entries, `{ name: "${NAME}" }`].sort((a, b) => {
      const na = /name:\s*"([^"]+)"/.exec(a)[1];
      const nb = /name:\s*"([^"]+)"/.exec(b)[1];
      return rank.get(na) - rank.get(nb);
    });

    const rebuilt = block[1] + "\n        " + withMine.join(",\n        ") + block[3];
    const newDay = day[1] + day[2].replace(block[0], rebuilt) + day[3];
    text = text.replace(day[0], newDay);
    added += 1;
  }
}

fs.writeFileSync(FILE, text);
console.log(`${NAME}: ${added} 枠を追加${already ? `（${already} 枠は既存）` : ""}`);

"use strict";

function emptyHalfMonthSchedules() {
  return {
    schemaVersion: 1, complete: false, checkedAt: null, lastSuccessAt: null,
    schedules: [], lastRun: { status: "never" }
  };
}

function halfMonthEvidence({ observedSameDay = false } = {}) {
  const day = "\u663c";
  const night = "\u591c";
  const ito = "\u3044\u3068";
  const observed = (id, date) => ({
    id, url: `https://x.com/akibazettai/status/${id}`,
    authorId: "822429861218131969", authorScreenName: "akibazettai",
    createdAt: `${date}T03:00:00Z`, observedAt: `${date}T05:00:00Z`,
    date, shift: day, storeId: "s1", names: [ito]
  });
  const personalPost = (id, name, authorScreenName, shifts) => ({
    id, url: `https://x.com/${authorScreenName}/status/${id}`,
    name, authorId: id, authorScreenName, date: "2026-09-06",
    createdAt: "2026-09-06T01:00:00Z", observedAt: "2026-09-06T02:00:00Z",
    events: shifts.map(([shift, storeId]) => ({
      shift, kind: "placement", storeId, excerpt: "synthetic placement"
    }))
  });
  const metadata = () => ({
    schemaVersion: 1, complete: false, checkedAt: "2026-09-07T05:00:00Z",
    lastSuccessAt: "2026-09-07T05:00:00Z", lastRun: { status: "ok" }
  });
  return {
    official: {
      ...metadata(), pending: [],
      posts: [
        observed("2096000000000000001", "2026-09-05"),
        ...(observedSameDay ? [observed("2097000000000000001", "2026-09-07")] : [])
      ]
    },
    personal: {
      ...metadata(),
      posts: [
        personalPost("2096500000000000001", "\u3042\u3080", "amu_zettai", [[day, "s1"], [night, "s2"]]),
        personalPost("2096500000000000002", "\u3089\u3089\u3053", "rarako_zettai", [[day, "s1"]])
      ]
    }
  };
}

module.exports = { emptyHalfMonthSchedules, halfMonthEvidence };

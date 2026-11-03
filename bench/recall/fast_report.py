"""Is the fast path converging on the judge? From a team's own record.

    python bench/recall/fast_report.py <team_dir> [--agent coder]
    python bench/recall/fast_report.py <team_dir> --replay [--every 25]

The fast path (ateam/recall_fast.py) runs in shadow beside the judge and
records, with each recall, what it would have offered. The report counts, by
day, how often it chose what the judge chose, when the judge chose something,
and how often it offered something when the judge chose nothing. It also
counts the judge's choices that only reached it because the fast path put
them among the candidates.

--replay starts a fast path from nothing and runs it over the recorded
requests in order: predict, compare with the judge, then learn from the
judge and from the misses and verdicts that followed, as the team's did. It
uses hashed words, never a model, so it costs nothing and can be run after
any change to recall_fast.py to see whether the change converges faster or
further on real requests. Two limits: memories are as they are today, not
as they were at each request; and only recalls recorded since the fast path
shipped carry their request.

It writes nothing to the team.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from ateam import recall, recall_fast  # noqa: E402


def events(team_dir: Path) -> list[dict]:
    out = []
    try:
        with (team_dir / "events.jsonl").open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def judged(e: dict) -> bool:
    return e.get("event") == "recall" and "+judge" in str(e.get("how") or "")


def tally(rows: list[tuple[set, set, set]]) -> dict:
    """rows: (judge's picks, fast picks, explored)."""
    chose = [r for r in rows if r[0]]
    quiet = [r for r in rows if not r[0]]
    return {"wakes": len(rows), "chose": len(chose),
            "matched": sum(1 for j, f, _ in chose if j == f),
            "found": sum(1 for j, f, _ in chose if j & f),
            "false_alarm": sum(1 for _, f, _ in quiet if f),
            "via_explore": sum(1 for j, _, x in chose if j & x)}


def line(label: str, t: dict, explored: bool = True) -> str:
    pct = lambda a, b: f"{100 * a / b:.0f}%" if b else "-"   # noqa: E731
    return (f"{label:<12} {t['wakes']:>5} wakes  judge chose {t['chose']:>4}  "
            f"fast matched {pct(t['matched'], t['chose']):>4}  "
            f"found one {pct(t['found'], t['chose']):>4}  "
            f"offered when quiet {pct(t['false_alarm'], t['wakes'] - t['chose']):>4}"
            + (f"  judge's choice came by exploring {t['via_explore']}" if explored else ""))


def report(rows: list[dict], agent: str | None) -> list[str]:
    by_day = defaultdict(list)
    for e in rows:
        if judged(e) and e.get("fast") and (not agent or e.get("agent") == agent):
            by_day[str(e.get("ts") or "")[:10]].append(
                (set(e.get("picked") or []), set(e["fast"].get("picks") or []),
                 set(e["fast"].get("explored") or [])))
    if not by_day:
        return ["No recall in this record carries the fast path's picks: it ships in a-team "
                "4.7.0, and runs when memory_recall is on."]
    out = [line(day, tally(r)) for day, r in sorted(by_day.items())]
    out.append(line("all", tally([x for r in by_day.values() for x in r])))
    return out


class _NoModel:
    available = False
    can_embed = False


def replay(team_dir: Path, rows: list[dict], agent: str | None, every: int) -> list[str]:
    """A fresh fast path per agent over the recorded requests, in order."""
    out = []
    agents = sorted({e["agent"] for e in rows if judged(e) and e.get("query")
                     and (not agent or e.get("agent") == agent)})
    if not agents:
        return ["No recorded recall carries its request, so there is nothing to replay."]
    with tempfile.TemporaryDirectory() as tmp:
        for name in agents:
            ar = recall.AgentRecall(team_dir, name, {**recall.DEFAULTS, "fast": "shadow"},
                                    backend=_NoModel())
            mems = {m["id"]: m for m in ar.memories()}
            cache = ar._cache()
            mvecs = {mid: recall_fast.hashed(ar._doc_text(m, cache.get(mid) or {}))
                     for mid, m in mems.items()}
            model = recall_fast.Fast(Path(tmp) / f"{name}.fast.json", f"hashed:{recall_fast.HASH_DIMS}")
            queries, results = {}, []

            def teach(query, labels):
                qv = recall_fast.hashed(query[:6000])
                words = {r["id"]: r["norm"] for r in ar._by_words(list(mems.values()), cache, query, len(mems))}
                feats = {mid: model.features(qv, mid, mvecs.get(mid), words.get(mid, 0.0))
                         for mid, _, _ in labels if mid in mems}
                model.learn(qv, [l for l in labels if l[0] in mems], feats)

            for e in rows:
                if e.get("agent") != name:
                    continue
                kind = e.get("event")
                if judged(e) and e.get("query"):
                    q = e["query"]
                    queries[e.get("rid")] = q
                    qv = recall_fast.hashed(q[:6000])
                    words = {r["id"]: r["norm"] for r in ar._by_words(list(mems.values()), cache, q, len(mems))}
                    ranked = model.rank(qv, {m: mvecs[m] for m in mems}, words)
                    picks = set(recall_fast.Fast.picks(ranked, recall.DEFAULTS["max"]))
                    chosen = set(e.get("picked") or []) & set(mems)
                    results.append((chosen, picks, set()))
                    feats = {r["id"]: r["f"] for r in ranked}
                    labels = [(c, int(c in chosen), recall_fast.JUDGE_W)
                              for c in e.get("candidates") or [] if c in mems]
                    model.learn(qv, labels, feats, judged=True)
                elif kind in ("recall_miss", "recall_ignored") and e.get("recall") in queries:
                    teach(queries[e["recall"]], [(e.get("memory"), 1, recall_fast.MISS_W)])
                elif kind == "recall_used" and e.get("recall") in queries:
                    y = {"used": 1, "ignored": 1, "na": 0}.get(e.get("verdict"))
                    if y is not None:
                        teach(queries[e["recall"]], [(e.get("memory"), y, recall_fast.USED_W)])
            out.append(f"{name}: {len(mems)} memories, {len(results)} judged requests")
            for i in range(0, len(results), every):
                out.append(line(f"  {i + 1}-{i + len(results[i:i + every])}",
                                tally(results[i:i + every]), explored=False))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("team_dir", type=Path)
    ap.add_argument("--agent")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--every", type=int, default=25)
    a = ap.parse_args(argv)
    rows = events(a.team_dir)
    lines = replay(a.team_dir, rows, a.agent, a.every) if a.replay else report(rows, a.agent)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Would recalling during a turn have caught the misses recall had before it?

    python bench/recall/late_misses.py <team_dir> [--every 10] [--max-queries 6]

Recall runs once, before a turn, on the words of the mail. A miss is a
correction that restated a memory the agent was not shown. This takes each miss
in a team's record, replays what the agent was doing in the turn it corrects as
if recall had run every --every tool calls, and counts the misses that replay
finds. It answers whether mid-turn recall is worth a judge call per checkpoint,
from a team's real record, before anything is built.

It writes nothing to the team. It runs the team's own recall config, so it
needs the same key or judge command the team uses, and it stops at
RECALL_BUDGET_USD like the rest of the bench. Works on an agy-team directory too.

Two limits on the answer. The memories are as they are today, not as they were
at the miss, and a miss has already taught the memory cues, so a caught count
leans high. A runner that records no tool calls leaves nothing to replay: those
misses are counted as unknown, never as not caught.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from ateam import recall  # noqa: E402

CALL_CHARS = 400
QUERY_CHARS = 6000
#: Rough list price of one replay query: an embedding plus a judge call.
QUERY_USD = 0.002


def events(team_dir: Path) -> list[dict]:
    out = []
    with (team_dir / "events.jsonl").open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def missed_turns(rows: list[dict]) -> list[dict]:
    """Each miss, with the tool calls made since the recall before it.

    A wake's order in the record is recall, the turn's calls, then the next
    wake's recall, and the miss is logged before that recall: so the calls
    between one agent's last recall and its miss are the turn being corrected.
    """
    out, calls, seen = [], {}, set()
    for e in rows:
        agent, kind = e.get("agent"), e.get("event")
        if kind == "recall":
            calls[agent] = []
            seen.add(agent)
        elif kind == "tool_call" and agent in seen:
            calls[agent].append(e)
        elif kind == "recall_miss":
            out.append({**e, "calls": list(calls.get(agent, []))})
    return out


def describe(call: dict) -> str:
    args = call.get("args")
    text = json.dumps(args, default=str) if not isinstance(args, str) else args
    return f"{call.get('tool')}: {text[:CALL_CHARS]}"


def queries(calls: list[dict], every: int, most: int) -> list[str]:
    """What recall would have been asked at each checkpoint: the calls since
    the last one. Evenly spread when a long turn has too many to afford."""
    chunks = [calls[i:i + every] for i in range(0, len(calls), every)]
    if len(chunks) > most:
        step = len(chunks) / most
        chunks = [chunks[int(i * step)] for i in range(most)]
    return ["\n".join(describe(c) for c in chunk)[:QUERY_CHARS] for chunk in chunks]


def replay(team_dir: Path, agent: str, memory: str, qs: list[str], cfg: dict) -> dict:
    """Whether any checkpoint reached the memory, and whether the judge took it."""
    ar = recall.AgentRecall(team_dir, agent, cfg)
    reached = picked = False
    for q in qs:
        res = ar.recall(q)
        reached = reached or memory in (res.get("candidates") or [])
        picked = picked or memory in {m["id"] for m in res.get("picked") or []}
        if picked:
            break
    return {"reached": reached, "picked": picked}


def report(misses: list[dict], results: dict[int, dict], wakes: int) -> list[str]:
    n = len(misses)
    known = [i for i in range(n) if i in results]
    picked = sum(results[i]["picked"] for i in known)
    reached = sum(results[i]["reached"] for i in known)
    lengths = sorted(len(m["calls"]) for m in misses)
    lines = [f"{wakes} wakes with recall, {n} misses"
             + (f" ({100 * n / wakes:.1f} per 100 wakes)" if wakes else "")]
    if not n:
        return lines + ["Nothing to replay: no misses in this record."]
    lines.append(f"tool calls in the corrected turn: median {lengths[n // 2]}, longest {lengths[-1]}")
    lines.append(f"unknown (no tool calls recorded): {n - len(known)}")
    if known:
        lines.append(f"caught mid-turn: {picked} of {len(known)} ({100 * picked / len(known):.0f}%); "
                     f"reached the candidates: {reached}")
    for agent, c in Counter(m.get("agent") for m in misses).most_common():
        hits = sum(results[i]["picked"] for i in known if misses[i].get("agent") == agent)
        lines.append(f"  {agent}: {c} misses, {hits} caught")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("team_dir", type=Path)
    ap.add_argument("--every", type=int, default=10, help="tool calls between checkpoints")
    ap.add_argument("--max-queries", type=int, default=6, help="checkpoints per miss at most")
    ap.add_argument("--dry-run", action="store_true", help="count and price it, call no model")
    a = ap.parse_args(argv)
    rows = events(a.team_dir)
    misses = missed_turns(rows)
    wakes = sum(e.get("event") == "recall" for e in rows)
    plans = {i: queries(m["calls"], a.every, a.max_queries)
             for i, m in enumerate(misses) if m["calls"]}
    total = sum(len(q) for q in plans.values())
    budget = float(__import__("os").environ.get("RECALL_BUDGET_USD", "10"))
    print(f"{total} replay queries, about ${total * QUERY_USD:.2f}")
    if total * QUERY_USD > budget:
        print(f"over the ${budget:.2f} budget (RECALL_BUDGET_USD); stopping", file=sys.stderr)
        return 1
    results: dict[int, dict] = {}
    if not a.dry_run and plans:
        cfg = recall.config(a.team_dir) or {}
        for i, qs in plans.items():
            results[i] = replay(a.team_dir, misses[i].get("agent"), misses[i].get("memory"), qs, cfg)
    print("\n".join(report(misses, results, wakes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Fast recall: a scorer with no model call, trained by the judge and by what
happened after, run in shadow beside the judge.

Asked for: an immediate answer, then the judge's, with what the judge and the
team's corrections teach folded back into the fast path so it converges, and
stays stable as memories pile up. These use a fake model; bench/recall/
fast_report.py measures a real team's record.
"""
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam import recall, recall_fast  # noqa: E402
from ateam.supervisor import Supervisor  # noqa: E402
from ateam.transport import load as load_transport  # noqa: E402
from fixture_runner import ScriptedRunner  # noqa: E402
from test_recall import AGENTS, Fake, save, team  # noqa: E402,F401


class Judge(Fake):
    """A judge that knows which memory each kind of request needs, by words
    the memory itself never uses: what a model knows and word search does not."""

    def __init__(self, table):
        super().__init__()
        self.table, self.prompts = table, []

    def ask(self, prompt):
        if not prompt.startswith("An AI agent on a team"):
            return super().ask(prompt)
        self.prompts.append(prompt)
        msg = prompt.split("<message>")[1].split("</message>")[0]
        offered = {b.split('">', 1)[0] for b in prompt.split('<memory id="')[1:]}
        return {"apply": [mid for cue, mid in self.table.items() if cue in msg and mid in offered][:1]}


TOPICS = ["parser", "deploy", "billing", "cache", "schema", "auth", "metrics", "queue"]
OBLIQUE = {"parser": "grammar tokens lexing", "deploy": "rollout canary fleet",
           "billing": "invoice proration refund", "cache": "staleness eviction ttl",
           "schema": "column backfill nullable", "auth": "login session cookie",
           "metrics": "dashboard percentile alerting", "queue": "backlog consumer redelivery"}


def corpus(team):
    for t in TOPICS:
        save(team, "coder", f"{t}-lesson", f"A lesson about the {t}",
             f"When working on the {t}, check the {t} edge cases first.")
    return {OBLIQUE[t].split()[0]: f"{t}-lesson" for t in TOPICS}


def stream(n, seed=1):
    """Requests that name a topic's work obliquely, and some that need nothing."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        if rng.random() < 0.25:
            out.append(("Please update the readme wording and fix a typo", None))
        else:
            t = rng.choice(TOPICS)
            words = OBLIQUE[t].split()
            rng.shuffle(words)
            out.append((f"Can you look at the {' '.join(words)} problem today?", f"{t}-lesson"))
    return out


def run(ar, n=240):
    """Per wake: (the judge chose a memory, the fast path chose the same, the
    judge chose the one the request needed)."""
    out = []
    for q, want in stream(n):
        res = ar.recall(q)
        judged = {m["id"] for m in res["picked"]}
        out.append((bool(judged), set(res["fast"]["picks"]) == judged, want in judged))
    return out


def matched(rows):
    """How often the fast path chose what the judge chose, when it chose."""
    chose = [r for r in rows if r[0]]
    return sum(r[1] for r in chose) / max(1, len(chose))


def test_the_fast_path_converges_on_what_the_judge_chooses(team):
    judge = Judge(corpus(team))
    ar = recall.AgentRecall(team, "coder", {**recall.DEFAULTS, "candidates": 3}, backend=judge)
    ar.refresh()
    rows = run(ar)
    first, last = matched(rows[:40]), matched(rows[-40:])
    assert first < 0.6, f"it matched {first:.0%} before learning: the test proves nothing"
    assert last >= 0.95, f"matching went {first:.0%} -> {last:.0%}"
    quiet = [r for r in rows[-40:] if not r[0]]
    assert all(r[1] for r in quiet), "the fast path offered something for a request that needed nothing"


def test_its_own_picks_bring_the_judge_what_the_candidates_missed(team):
    """Candidates by meaning cannot find a memory whose words a request never
    uses; what the fast path learned can, and the judge then sees it."""
    judge = Judge(corpus(team))
    ar = recall.AgentRecall(team, "coder", {**recall.DEFAULTS, "candidates": 3}, backend=judge)
    ar.refresh()
    rows = run(ar)
    assert sum(r[2] for r in rows[-40:]) > sum(r[2] for r in rows[:40])


def test_the_judge_also_grades_the_fast_paths_own_top_picks(team):
    judge = Judge(corpus(team))
    ar = recall.AgentRecall(team, "coder", {**recall.DEFAULTS, "candidates": 1}, backend=judge)
    ar.refresh()
    res = ar.recall("Can you look at the rollout canary fleet problem today?")
    assert len(res["candidates"]) == 3 and res["fast"]["explored"] == res["candidates"][1:]
    assert all(f'<memory id="{i}">' in judge.prompts[-1] for i in res["candidates"])


def test_what_it_keeps_stays_bounded_however_long_it_learns(team):
    judge = Judge(corpus(team))
    ar = recall.AgentRecall(team, "coder", {**recall.DEFAULTS, "candidates": 3}, backend=judge)
    ar.refresh()
    for q, _ in stream(300, seed=7):
        ar.recall(q)
    st = json.loads((team / "recall" / "coder.fast.json").read_text())
    assert st["n"] > 300
    for e in st["mem"].values():
        assert len(e.get("pos") or []) <= recall_fast.PROTOS and len(e.get("neg") or []) <= recall_fast.PROTOS
        assert all(len(p["v"]) == recall_fast.PROTO_DIMS for p in (e.get("pos") or []) + (e.get("neg") or []))
    assert all(abs(w) <= recall_fast.CLIP for w in st["w"].values())


def test_a_deleted_memory_leaves_the_fast_path_too(team):
    judge = Judge(corpus(team))
    ar = recall.AgentRecall(team, "coder", {**recall.DEFAULTS, "candidates": 3}, backend=judge)
    ar.refresh()
    for q, _ in stream(30):
        ar.recall(q)
    from ateam import mcp_memory, memory_log
    mcp_memory.handlers(memory_log.open_store(team, "coder"), team)["delete_memory"]({"name": "deploy-lesson"})
    ar.recall("Can you look at the rollout canary fleet problem today?")
    assert "deploy-lesson" not in json.loads((team / "recall" / "coder.fast.json").read_text())["mem"]


def test_off_means_no_fast_path_and_nothing_added_for_the_judge(team):
    judge = Judge(corpus(team))
    ar = recall.AgentRecall(team, "coder", {**recall.DEFAULTS, "candidates": 1, "fast": "off"},
                            backend=judge)
    ar.refresh()
    res = ar.recall("Can you look at the rollout canary fleet problem today?")
    assert "fast" not in res and len(res["candidates"]) == 1
    assert not (team / "recall" / "coder.fast.json").exists()


def test_a_new_vector_space_starts_afresh(team, tmp_path):
    f = recall_fast.Fast(tmp_path / "a.fast.json", "hashed:256")
    f.learn(recall_fast.hashed("a b c"), [("m", 1, 1.0)], {"m": [0.1, 0.0, 0.0, 0.0, 0.0]})
    f.save()
    assert recall_fast.Fast(tmp_path / "a.fast.json", "hashed:256").mem
    assert not recall_fast.Fast(tmp_path / "a.fast.json", "embed:gemini-embedding-2:768").mem


def test_without_an_embedder_it_works_on_words(team):
    class WordsOnly(Judge):
        can_embed = False

        def embed(self, texts, task):
            raise RuntimeError("no embedder")
    judge = WordsOnly(corpus(team))
    ar = recall.AgentRecall(team, "coder", {**recall.DEFAULTS, "candidates": 3}, backend=judge)
    rows = run(ar)
    assert ar.recall("x y")["fast"]["space"] == "hashed:256"
    assert matched(rows[-40:]) >= 0.9, matched(rows[-40:])


def recall_on(team, **cfg):
    doc = json.loads((team / "roster.json").read_text())
    doc["memory_recall"] = cfg or True
    (team / "roster.json").write_text(json.dumps(doc))


def test_in_shadow_the_agent_sees_only_the_judges_choice_and_the_record_has_both(team, monkeypatch):
    recall_on(team)
    judge = Judge(corpus(team))
    monkeypatch.setattr(recall, "Gemini", lambda cfg: judge)
    seen = []

    class Sees(ScriptedRunner):
        def wake(self, agent, message):
            seen.append(message)
            return "ok"
    # Names the deploy, so word search finds it before any index is written.
    load_transport("user").send("coder", "Can you look at the deploy rollout canary fleet problem today?")
    sup = Supervisor(AGENTS, Sees({}), quiet=True, require_review=False)
    try:
        sup.run_until_idle()
    finally:
        sup.close()
    ev = [json.loads(l) for l in (team / "events.jsonl").read_text().splitlines()]
    rec = [e for e in ev if e.get("event") == "recall"][-1]
    assert rec["query"].startswith("Can you look at the deploy rollout") and "picks" in rec["fast"]
    assert rec["picked"] == ["deploy-lesson"] and "deploy-lesson" in seen[0]
    shown = {m for m in rec["fast"]["picks"]} - set(rec["picked"])
    assert not any(f"({m})" in seen[0] for m in shown), "a fast pick reached the agent in shadow"


def test_a_memory_the_agent_opened_itself_teaches_the_fast_path(team, monkeypatch):
    recall_on(team)
    judge = Judge({})                       # a judge that never picks
    monkeypatch.setattr(recall, "Gemini", lambda cfg: judge)
    corpus(team)

    class Opens(ScriptedRunner):
        def wake(self, agent, message):
            self.observer.record_tool_call(agent=agent, conversation="c",
                                           tool="mcp__ateam_memory__read_memory",
                                           args={"name": "deploy-lesson"}, result="...")
            return "ok"
    load_transport("user").send("coder", "Can you look at the rollout canary fleet problem today?")
    sup = Supervisor(AGENTS, Opens({}), quiet=True, require_review=False)
    try:
        sup.run_until_idle()
        sup._bg.shutdown(wait=True)
    finally:
        sup.close()
    st = json.loads((team / "recall" / "coder.fast.json").read_text())
    assert st["mem"]["deploy-lesson"]["pos"], "the memory it opened was not taught"


def test_a_used_check_saying_it_did_not_bear_teaches_a_negative(team):
    corpus(team)
    ar = recall.AgentRecall(team, "coder", {**recall.DEFAULTS}, backend=Judge({}))
    ar.refresh()
    ar.teach("Please update the readme wording", [("deploy-lesson", 0, recall_fast.USED_W)])
    st = json.loads((team / "recall" / "coder.fast.json").read_text())
    assert st["mem"]["deploy-lesson"]["neg"] and not st["mem"]["deploy-lesson"]["pos"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))


def test_the_report_and_a_replay_read_the_teams_own_record(team, monkeypatch):
    sys.path.insert(0, str(ROOT / "bench" / "recall"))
    import fast_report
    from ateam.transport import Message
    recall_on(team, candidates=3)
    judge = Judge(corpus(team))
    monkeypatch.setattr(recall, "Gemini", lambda cfg: judge)
    sup = Supervisor(AGENTS, ScriptedRunner({}), quiet=True, require_review=False)
    try:
        cfg = recall.config(team)
        recall.AgentRecall(team, "coder", cfg).refresh()
        for q, _ in stream(150):
            sup._recall_block("coder", [Message(ts="t", sender="user", to="coder", content=q)], cfg)
    finally:
        sup.close()
    rows = fast_report.events(team)
    lines = fast_report.report(rows, "coder")
    assert lines[-1].startswith("all") and " 150 wakes" in lines[-1]
    out = fast_report.replay(team, rows, "coder", every=50)
    assert out[0].startswith("coder: 8 memories, 150 judged requests")
    first, last = (int(l.split("fast matched")[1].split("%")[0]) for l in (out[1], out[-1]))
    assert last > first and last >= 90, out
    assert (team / "recall" / "coder.fast.json").exists() and len(list((team / "recall").glob("*.fast.json"))) == 1

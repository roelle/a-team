"""Triggers judged by what happened after, and learned from it.

Asked for: "Yes on suggest. Nothing says that the memory needs to match the
command or words in the prompt. The goal is to pull information that is
useful from disk. I don't have religion about the recall algorithm. I want
it judged post facto. It's going to get called over and over again, so that
starts to look like a control algorithm, something with feedback. We want
it converging over time to the best memories that are available."

These pin the loop: a call that went wrong and was made again gives the
memory that would have helped a trigger, whether the agent pointed at it
(by opening it) or a judge did; a memory shown before calls it does not
bear on stops being shown, whoever wrote the trigger, and the same
evidence brings it back; a memory that was out of sight when its call went
wrong is shown again; and where no memory would have helped, the agent is
told what went wrong and what worked, when it next writes down lessons.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam import mcp_memory, memory_log, trigger_learn as learn, triggers  # noqa: E402
from ateam.recall import AgentRecall  # noqa: E402

KUBECTL = ("Always pass --context staging; the default context is production. "
           "`kubectl apply` needs -n web or it lands in default.")
NO_RESOURCE = ('Exit code 1\nerror: the server doesn\'t have a resource type "deployments" '
               'in namespace "default"')
PROTECTED = "Exit code 1\nremote: error: GH006: Protected branch update failed for refs/heads/main"


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "teams" / "demo" / "team"
    (td / "inbox").mkdir(parents=True)
    (td / "roster.json").write_text(json.dumps({"agents": [
        {"name": a, "role": "x"} for a in ("tpm", "coder")]}))
    (td / "conversations.json").write_text(json.dumps({"coder": "conv-1"}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(td.parent))
    monkeypatch.setenv("ATEAM_MODEL_CHECK", "0")
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    for var in ("ATEAM_WORKSPACE", "ATEAM_MEMORY_STORE", "ATEAM_MEMORY_CONFIG", "ATEAM_OBSERVER",
                "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    triggers.forget_cache()
    yield td
    triggers.forget_cache()


class Judge:
    """Answers in order; keeps what it was asked."""
    available = True

    def __init__(self, *answers, always=None):
        self.answers, self.always, self.prompts = list(answers), always, []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else (self.always or {})


def tools(team, agent="coder"):
    return mcp_memory.handlers(memory_log.open_store(team, agent), team)


def save(team, name, content, description="d", **more):
    out = tools(team)["save_memory"]({"name": name, "description": description,
                                      "content": content, **more})
    triggers.forget_cache()
    return out


def call(team, what, error=None, tool="Bash", agent="coder"):
    """One finished call on the team's record, as a runner writes it."""
    args = {"command": what} if isinstance(what, str) else what
    with (team / "events.jsonl").open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "tool_call",
                            "agent": agent, "tool": tool, "args": args,
                            "result": None if error else "ok", "error": error}) + "\n")


def shown(team, memory, via, cmd):
    """A memory shown before a call, as triggers.check() records it."""
    with (team / "events.jsonl").open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "triggered",
                            "agent": "coder", "tool": "Bash", "shown": [f"memory:{memory}"],
                            "via": [via], "call": cmd,
                            "family": triggers.shape("Bash", {"command": cmd}, sub=False)}) + "\n")


def mark(team) -> int:
    p = team / "events.jsonl"
    return len(p.read_text().splitlines()) if p.exists() else 0


def turn(team, start: int, judge=None, record=None) -> list[str]:
    """Learn from what is on the record since `start`."""
    events = [json.loads(l) for l in (team / "events.jsonl").read_text().splitlines()[start:]]
    out = learn.observe(team, "coder", events, judge, record)
    triggers.forget_cache()
    return out


def hold(team, cmd):
    triggers.forget_cache()
    return triggers.hold(team, "coder", "Bash", {"command": cmd})


def wrong_then_right(team, opened=None):
    n = mark(team)
    call(team, "kubectl apply -f web.yaml", error=NO_RESOURCE)
    if opened:
        call(team, {"name": opened}, tool="mcp__ateam_memory__read_memory")
    call(team, "kubectl --context staging apply -f web.yaml -n web")
    return n


# --- a call that went wrong gives the memory that would have helped a trigger --------

def test_a_memory_opened_after_a_failed_call_comes_up_before_the_next_one(team):
    save(team, "kubectl-context", KUBECTL, "Before applying to the cluster")
    assert hold(team, "kubectl apply -f web.yaml") == "", "no trigger: nothing holds it"
    lines = turn(team, wrong_then_right(team, opened="kubectl-context"))
    assert any("'kubectl-context' now comes up before Bash(kubectl apply)" in l for l in lines), lines
    assert hold(team, "kubectl get pods") == "", "learned for the call that went wrong, no wider"
    held = hold(team, "cd deploy && sudo kubectl apply -f api.yaml")
    assert held.startswith("[held: this Bash call did not run") and KUBECTL in held
    assert "kubectl-context" in [m["name"] for m in AgentRecall(team, "coder", {}).memories()], \
        "nobody took it out of wake recall"
    assert "Bash(kubectl apply) [learned]" in tools(team)["memory_index"]({})


def test_a_judge_names_the_memory_and_the_trigger_from_what_happened(team):
    save(team, "release-steps", "A stuck release: `helm rollback web` first, then upgrade "
         "with --atomic so a failed one undoes itself.", "Shipping the web chart")
    save(team, "user-prefs", "Short replies.", "Writing to the user")
    n = mark(team)
    call(team, "helm upgrade web ./chart", error="Error: UPGRADE FAILED: another operation "
         "(install/upgrade/rollback) is in progress")
    call(team, "helm rollback web")
    judge = Judge({"memory": "release-steps", "trigger": "Bash(helm *)"})
    lines = turn(team, n, judge)
    assert any("'release-steps' now comes up before Bash(helm *)" in l for l in lines), lines
    asked = judge.prompts[0]
    assert "helm upgrade web ./chart" in asked and "another operation" in asked \
        and "helm rollback web" in asked and "went through" in asked, \
        "the judge sees the call, what came back and what worked"
    assert "release-steps" in asked and "user-prefs" in asked, \
        "every memory is a candidate: none is left out for not sharing the call's words"
    assert hold(team, "helm list").startswith("[held")


@pytest.mark.parametrize("answer", ["Bash", "Bash(*)", "Bash(docker *)", "not a trigger!", None])
def test_a_trigger_the_judge_gets_wrong_is_the_call_itself(team, answer):
    save(team, "release-steps", "Upgrade with --atomic.")
    n = mark(team)
    call(team, "helm upgrade web ./chart", error="Error: UPGRADE FAILED: timed out waiting")
    call(team, "helm upgrade web ./chart --atomic")
    lines = turn(team, n, Judge({"memory": "release-steps", "trigger": answer}))
    assert any("before Bash(helm upgrade)" in l for l in lines), lines
    assert hold(team, "ls") == "", "never before every call there is"


def test_the_same_program_from_another_place_is_the_same_call_made_again(team):
    save(team, "suite-python", "Use .venv/bin/python -m pytest -q; the system python lacks the deps.")
    n = mark(team)
    call(team, "python -m pytest -q evals/test_x.py",
         error="Exit code 1\n/usr/bin/python: No module named pytest")
    call(team, {"name": "suite-python"}, tool="read_memory")
    call(team, "cd /repo && .venv/bin/python -m pytest -q evals/test_x.py")
    lines = turn(team, n)
    assert any("'suite-python' now comes up before Bash(python -m pytest)" in l for l in lines), lines
    assert hold(team, "python -c 'print(1)'") == "", "not before every python call"
    assert hold(team, "/usr/bin/python -m pytest -q").startswith("[held"), \
        "a trigger names the program, wherever it is run from"


def test_with_no_memory_for_it_the_agent_is_told_what_went_wrong_and_what_worked(team):
    save(team, "user-prefs", "Short replies.")
    n = mark(team)
    call(team, "helm upgrade web ./chart", error="Error: UPGRADE FAILED: timed out waiting")
    call(team, "helm upgrade web ./chart --atomic --timeout 10m")
    lines = turn(team, n, Judge({"memory": None, "trigger": None}))
    assert lines == ["Bash(helm upgrade) took 2 tries, with no memory on it"]
    told = learn.missing(team, "coder")
    assert "Bash(helm upgrade): 2 tries" in told and "timed out waiting" in told \
        and "What worked: `helm upgrade web ./chart --atomic --timeout 10m`" in told
    assert 'triggers="Bash(helm upgrade)"' in told and "without looking anything up" in told
    assert learn.missing(team, "coder") == "", "told once"
    assert "Bash(helm upgrade): 2 tries" in learn.review(team, "coder", "")
    save(team, "helm-upgrade", "Pass --atomic --timeout 10m.", triggers="Bash(helm upgrade)")
    assert "helm upgrade" not in learn.review(team, "coder", ""), "it has a memory now"


def test_looking_up_how_to_make_a_call_is_a_lesson_missing_too(team):
    n = mark(team)
    call(team, "helm upgrade --help")
    call(team, "helm upgrade web ./chart --atomic")
    turn(team, n)
    told = learn.missing(team, "coder")
    assert "Bash(helm upgrade): 2 tries" in told and "looked up how to make the call" in told


def test_ordinary_work_is_not_a_call_that_went_wrong(team):
    n = mark(team)
    # A test that failed until the code was fixed.
    call(team, "python -m pytest -q", error="Exit code 1\nFAILED tests/test_api.py::test_get - "
         "AssertionError: assert 404 == 200")
    call(team, {"file_path": "/repo/api.py", "old_string": "a", "new_string": "b"}, tool="Edit")
    call(team, "python -m pytest -q -k test_get")
    # A search that found nothing: an answer, not a mistake.
    call(team, "grep -rn frobnicate .", error="Exit code 1")
    call(team, "grep -rn frob .")
    # The same call again: the network, not the agent.
    call(team, "curl -sf https://example.test/health", error="Exit code 7\ncurl: (7) Failed "
         "to connect to example.test port 443")
    call(team, "curl -sf https://example.test/health")
    events = [json.loads(l) for l in (team / "events.jsonl").read_text().splitlines()[n:]]
    assert learn.episodes(events) == []
    assert turn(team, n, Judge()) == [] and learn.missing(team, "coder") == ""


# --- a memory shown before calls it does not bear on stops being shown ---------------

def mute_git(team) -> int:
    save(team, "git-push-rules", "Never push to main: push a branch and open a PR.",
         "Before pushing", triggers="Bash(git *)")
    judge = Judge(always={"verdict": "na", "evidence": "a status check, nothing about pushing"})
    for i in range(1, 9):
        n = mark(team)
        shown(team, "git-push-rules", "Bash(git *)", "git status")
        call(team, "git status")
        if any("no longer comes up before Bash(git *)" in l for l in turn(team, n, judge)):
            return i
    return 0


def test_a_written_trigger_that_never_bears_on_its_calls_is_muted_and_says_so(team):
    assert mute_git(team) == 5, "trusted until five straight verdicts say otherwise"
    assert hold(team, "git push origin main") == "" and hold(team, "git status") == ""
    assert "git-push-rules" in [m["name"] for m in AgentRecall(team, "coder", {}).memories()], \
        "with no trigger left it is recalled on a wake again"
    assert "Bash(git *) [not shown" in tools(team)["memory_index"]({})
    told = learn.review(team, "coder", "")
    assert "no longer shown: 'git-push-rules' before Bash(git *)" in told and "Narrow" in told
    assert "- Triggers: Bash(git *)" in memory_log.open_store(team, "coder").read("git-push-rules"), \
        "muted, not rewritten: the memory is the agent's"


def test_what_was_muted_comes_back_narrower_when_a_call_goes_wrong_without_it(team):
    mute_git(team)
    n = mark(team)
    call(team, "git push origin main", error=PROTECTED)
    call(team, "git push origin fix-1")
    judge = Judge({"memory": "git-push-rules", "trigger": "Bash(git push)"})
    lines = turn(team, n, judge)
    assert any("'git-push-rules' now comes up before Bash(git push)" in l for l in lines), lines
    assert hold(team, "git status") == "", "still not before every git call"
    assert "Never push to main" in hold(team, "git push origin main")


def test_a_learned_trigger_is_retired_by_two_verdicts_and_brought_back_by_the_next_failure(team):
    save(team, "kubectl-context", KUBECTL)
    turn(team, wrong_then_right(team, opened="kubectl-context"))
    na = Judge(always={"verdict": "na", "evidence": "a dry run"})
    for expect_gone in (False, True):
        n = mark(team)
        shown(team, "kubectl-context", "Bash(kubectl apply)", "kubectl apply --dry-run=client -f x")
        call(team, "kubectl apply --dry-run=client -f x")
        gone = any("no longer comes up" in l for l in turn(team, n, na))
        assert gone is expect_gone
    assert hold(team, "kubectl apply -f web.yaml") == ""
    lines = turn(team, wrong_then_right(team, opened="kubectl-context"))
    assert any("now comes up before Bash(kubectl apply)" in l for l in lines), lines
    assert hold(team, "kubectl apply -f web.yaml").startswith("[held")


def test_a_memory_that_bore_on_the_call_keeps_its_trigger(team):
    save(team, "kubectl-gotchas", KUBECTL, triggers="Bash(kubectl *)")
    judge = Judge(always={"verdict": "used", "evidence": "added --context staging"})
    for _ in range(8):
        n = mark(team)
        shown(team, "kubectl-gotchas", "Bash(kubectl *)", "kubectl apply -f web.yaml")
        call(team, "kubectl --context staging apply -f web.yaml")
        assert not any("no longer" in l for l in turn(team, n, judge))
    assert KUBECTL in judge.prompts[0] and "kubectl --context staging apply" in judge.prompts[0]
    rec = json.loads((team / "triggers" / "coder.learned.json").read_text())["scores"]
    (only,) = rec.values()
    assert only["written"] and learn.score(only) > 0.85


def test_the_record_forgets_so_it_follows_what_is_true_now(team):
    cfg = dict(triggers.DEFAULTS)
    state = {"scores": {}, "news": []}
    for _ in range(30):                     # useful for a long time
        learn._credit(state, cfg, "m", "Bash(x)", True, 1.0, 0.0, "used")
    flips = [learn._credit(state, cfg, "m", "Bash(x)", True, 0.0, 1.0, "na") for _ in range(30)]
    assert "muted" in flips and flips.index("muted") < 20, \
        "a long good record does not buy as long a bad one: counting every call ever " \
        "made, it would take over seventy"


# --- a memory out of sight when its call went wrong ----------------------------------

def test_a_call_that_goes_wrong_with_its_memory_out_of_sight_shows_it_again(team):
    save(team, "kubectl-gotchas", KUBECTL, triggers="Bash(kubectl *)")
    assert hold(team, "kubectl get pods").startswith("[held")
    assert hold(team, "kubectl apply -f web.yaml") == "", "shown once this conversation"
    lines = turn(team, wrong_then_right(team))      # hours later, with nothing shown
    assert any("'kubectl-gotchas' is shown again at the next Bash(kubectl apply)" in l
               for l in lines), lines
    assert hold(team, "kubectl apply -f api.yaml").startswith("[held")


def test_a_memory_that_was_shown_and_did_not_prevent_it_is_one_to_deepen(team):
    save(team, "kubectl-gotchas", "Be careful with kubectl.", triggers="Bash(kubectl *)")
    n = mark(team)
    assert hold(team, "kubectl apply -f web.yaml").startswith("[held")
    call(team, "kubectl apply -f web.yaml", error=NO_RESOURCE)
    call(team, "kubectl --context staging apply -f web.yaml -n web")
    turn(team, n)
    told = learn.missing(team, "coder")
    assert "You were shown 'kubectl-gotchas' before a call that then went wrong anyway" in told
    assert "'kubectl-gotchas'. Add what was missing" in learn.review(team, "coder", "")


def test_a_held_call_a_host_records_as_failed_is_not_a_try(team):
    """OpenCode reports the call its plugin refused as a failed call."""
    save(team, "kubectl-gotchas", KUBECTL, triggers="Bash(kubectl *)")
    n = mark(team)
    held = hold(team, "kubectl apply -f web.yaml")
    call(team, "kubectl apply -f web.yaml", error=held)
    call(team, "kubectl --context staging apply -f web.yaml -n web")
    said = []
    assert turn(team, n, None, lambda ev, **f: said.append(ev)) == []
    assert said == ["trigger_verdict"] and learn.missing(team, "coder") == "", \
        "the memory changed the call: nothing went wrong"


# --- the agent has the last word; and without a judge --------------------------------

def test_saving_a_memory_with_its_triggers_set_starts_what_was_learned_over(team):
    save(team, "kubectl-context", KUBECTL)
    turn(team, wrong_then_right(team, opened="kubectl-context"))
    save(team, "kubectl-context", KUBECTL, triggers="none")
    assert hold(team, "kubectl apply -f web.yaml") == ""
    assert "[learned]" not in tools(team)["memory_index"]({})
    turn(team, wrong_then_right(team, opened="kubectl-context"))
    save(team, "kubectl-context", KUBECTL)          # triggers left out: nothing decided
    assert hold(team, "kubectl apply -f web.yaml").startswith("[held")


def test_a_merge_keeps_what_was_learned_for_its_sources(team):
    save(team, "kubectl-context", KUBECTL)
    save(team, "kubectl-namespaces", "web lives in the web namespace.")
    turn(team, wrong_then_right(team, opened="kubectl-context"))
    tools(team)["merge_memories"]({"names": "kubectl-context, kubectl-namespaces",
                                   "into": "kubectl", "description": "d",
                                   "content": KUBECTL + " web lives in the web namespace."})
    assert triggers.of(memory_log.open_store(team, "coder").read("kubectl")) == ["Bash(kubectl apply)"]


def test_without_a_judge_only_the_record_speaks_and_nothing_is_muted(team):
    save(team, "kubectl-gotchas", KUBECTL, triggers="Bash(kubectl *)")
    said = []
    for _ in range(8):                              # shown, and the same call made again
        n = mark(team)
        shown(team, "kubectl-gotchas", "Bash(kubectl *)", "kubectl get pods")
        call(team, "kubectl get pods")
        assert turn(team, n, None, lambda ev, **f: said.append((ev, f))) == []
    assert said == [], "an unchanged call says nothing either way"
    n = mark(team)
    shown(team, "kubectl-gotchas", "Bash(kubectl *)", "kubectl apply -f web.yaml")
    call(team, "kubectl --context staging apply -f web.yaml")
    turn(team, n, None, lambda ev, **f: said.append((ev, f)))
    (ev, f), = said
    assert ev == "trigger_verdict" and f["helped"] == 0.5 and not f["judged"] \
        and f["why"] == "the call came back changed and went through"
    assert learn.judge_for(team) is None, "no recall, no judge: nobody is asked"


def test_it_can_be_turned_off_on_its_own(team):
    doc = json.loads((team / "roster.json").read_text())
    (team / "roster.json").write_text(json.dumps({**doc, "memory_triggers": {"learn": False}}))
    save(team, "kubectl-context", KUBECTL)
    assert turn(team, wrong_then_right(team, opened="kubectl-context")) == []
    assert hold(team, "kubectl apply -f web.yaml") == ""
    save(team, "kubectl-gotchas", KUBECTL, triggers="Bash(kubectl *)")
    assert hold(team, "kubectl apply -f web.yaml").startswith("[held"), "written triggers still hold"


# --- from the team's record, after every turn ---------------------------------------

def test_a_showing_is_recorded_with_the_call_and_the_trigger_it_came_up_by(team):
    save(team, "kubectl-gotchas", KUBECTL, triggers="Bash(helm *); Bash(kubectl *)")
    hold(team, "cd deploy && kubectl apply -f web.yaml")
    ev = json.loads((team / "events.jsonl").read_text().splitlines()[-1])
    assert ev["event"] == "triggered" and ev["via"] == ["Bash(kubectl *)"] \
        and ev["call"] == "cd deploy && kubectl apply -f web.yaml" and ev["family"] == "Bash(kubectl)"


def test_a_turns_record_is_this_agents_calls_since_it_began(team):
    call(team, "kubectl apply -f old.yaml", error=NO_RESOURCE)
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 1))
    for e in (team / "events.jsonl").read_text().splitlines():
        assert json.loads(e)["ts"] < since
    time.sleep(1.1)
    call(team, "ls", agent="tpm")
    wrong_then_right(team)
    got = learn.turn_events(team, "coder", since)
    assert [e["args"]["command"] for e in got] == [
        "kubectl apply -f web.yaml", "kubectl --context staging apply -f web.yaml -n web"]


def test_the_supervisor_learns_after_a_turn_and_asks_when_the_agent_writes_lessons_down(team):
    from ateam.supervisor import DISTILL_PROMPT, Supervisor
    from fixture_runner import ScriptedRunner
    save(team, "kubectl-context", KUBECTL)
    sup = Supervisor(["tpm", "coder"], ScriptedRunner({}), quiet=True)
    try:
        assert sup._distill_prompt("coder") == DISTILL_PROMPT, "nothing went wrong: nothing added"
        since = time.strftime("%Y-%m-%d %H:%M:%S")
        wrong_then_right(team, opened="kubectl-context")
        call(team, "helm upgrade web ./chart", error="Error: UPGRADE FAILED: timed out waiting")
        call(team, "helm upgrade web ./chart --atomic")
        sup._learn_triggers("coder", since)
        seen = [json.loads(l) for l in (team / "events.jsonl").read_text().splitlines()]
        assert [e["memory"] for e in seen if e["event"] == "trigger_learned"] == ["kubectl-context"]
        assert [(e["shape"], e["memory"]) for e in seen if e["event"] == "trigger_trouble"] == [
            ("Bash(kubectl apply)", "kubectl-context"), ("Bash(helm upgrade)", None)]
        prompt = sup._distill_prompt("coder")
        assert prompt.startswith(DISTILL_PROMPT) and "Bash(helm upgrade): 2 tries" in prompt \
            and "kubectl apply" not in prompt, "only what has no memory yet"
        assert "Bash(helm upgrade): 2 tries" in sup._memory_review_prompt("coder")
    finally:
        sup.close() if hasattr(sup, "close") else None


def test_every_finished_turn_is_learned_from():
    src = (ROOT / "ateam" / "supervisor.py").read_text()
    finish = src[src.index("    def _finish(self, agent: str, turn: dict, reply: str)"):]
    assert "self._background(self._learn_triggers, agent" in finish[:2500]
    assert src.count("self._distill_prompt(agent)") == 3, "every distill carries the missing lessons"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))

"""A second opinion from another vendor's model on approved code.

Asked for: model diversity where it pays most, a reviewer that does not share
the author's blind spots, without letting it decide anything. These run the
supervisor's own path against a local stand-in for the Responses API.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from ateam import second_review  # noqa: E402
from ateam.supervisor import Supervisor  # noqa: E402
from ateam.transport import load as load_transport  # noqa: E402
from fake_openai import FakeOpenAI  # noqa: E402
from fixture_runner import ScriptedRunner  # noqa: E402


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def api():
    fake = FakeOpenAI(["src/auth.py:3 lets an empty password through."])
    yield fake
    fake.close()


@pytest.fixture
def team(tmp_path, monkeypatch, api):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "auth.py").write_text("def ok(p):\n    return True\n")
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "start")
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    write_roster(td, repo, {"model": "gpt-6-astra",
                            "provider": {"base_url": api.url + "/v1"}})
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(tmp_path / "durable"))
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    return td, repo


def write_roster(td, repo, cfg):
    doc = {"workspaces": [str(repo)], "agents": [{"name": "coder", "role": "implements"},
                                                 {"name": "qa", "role": "reviews"}]}
    if cfg is not None:
        doc["second_review"] = cfg
    (td / "roster.json").write_text(json.dumps(doc))


def approve(td, verdict="approved", proof="tests/test_auth.py"):
    with (td / "reviews.jsonl").open("a") as f:
        f.write(json.dumps({"ts": "2026-11-02 10:00:00", "kind": "work", "reviewer": "qa",
                            "author": "coder", "what": "password check", "verdict": verdict,
                            "findings": "looks right", "proof_file": proof}) + "\n")


def second(td, change=None, review=True, **kw):
    """One episode's start, a change, a review, and the check after the turn."""
    sup = Supervisor(["coder", "qa"], ScriptedRunner({}), quiet=True)
    try:
        sup._second_start()
        if change:
            change()
        if review:
            approve(td, **kw)
        sup._check_second()
        if getattr(sup, "_pool", None) is not None:
            sup._pool.shutdown(wait=True)
    finally:
        sup.close()
    rows = [json.loads(l) for l in (td / "events.jsonl").read_text().splitlines()] \
        if (td / "events.jsonl").exists() else []
    return [e for e in rows if e.get("event") == "second_review"]


def qa_mail():
    bus = load_transport("qa")
    try:
        return [m for m in bus.peek() if m.sender == "supervisor"]
    finally:
        bus.close()


def edit(repo):
    def go():
        (repo / "src" / "auth.py").write_text("def ok(p):\n    return p is not None\n")
        (repo / "src" / "new.py").write_text("SECRET_CHECK = False\n")
    return go


def test_an_approved_change_gets_a_second_opinion_and_qa_is_told(team, api):
    td, repo = team
    ev = second(td, edit(repo))
    assert len(api.requests) == 1
    req = api.requests[0]
    assert req["model"] == "gpt-6-astra" and req["store"] is False
    assert "return p is not None" in req["input"], "the diff was not sent"
    assert "SECRET_CHECK" in req["input"], "a new file was not sent"
    assert "password check" in req["input"], "the review was not sent"
    assert ev[-1]["findings"] is True and ev[-1]["model"] == "gpt-6-astra"
    mail = qa_mail()
    assert len(mail) == 1 and mail[0].kind == "review"
    assert "empty password" in mail[0].content and "not a verdict" in mail[0].content


def test_no_findings_sends_nothing(team, api):
    td, repo = team
    api.script[:] = ["No findings."]
    ev = second(td, edit(repo))
    assert ev[-1]["findings"] is False and not qa_mail()


def test_only_approved_code_reviews_are_sent(team, api):
    td, repo = team
    second(td, edit(repo), verdict="changes_requested")
    second(td, edit(repo), proof="")
    assert not api.requests


def test_paths_limit_it_to_the_risky_changes(team, api):
    td, repo = team
    write_roster(td, repo, {"provider": {"base_url": api.url + "/v1"}, "paths": ["*migrations*"]})
    ev = second(td, edit(repo))
    assert not api.requests and ev[-1]["skipped"] == "no path asked for"


def test_off_unless_the_team_asks(team, api):
    td, repo = team
    write_roster(td, repo, None)
    assert second(td, edit(repo)) == [] and not api.requests


def test_no_change_since_the_episode_began_sends_nothing(team, api):
    td, _ = team
    ev = second(td)
    assert not api.requests and ev[-1]["skipped"] == "no change"


def test_no_key_is_recorded_not_raised(team, api, monkeypatch):
    td, repo = team
    monkeypatch.delenv("OPENAI_API_KEY")
    ev = second(td, edit(repo))
    assert "OPENAI_API_KEY is not set" in ev[-1]["error"] and not api.requests


def test_a_plain_http_endpoint_is_refused():
    cfg = {**second_review.DEFAULTS, "provider": {"base_url": "http://example.com/v1",
                                                  "env_key": "OPENAI_API_KEY"}}
    with pytest.raises(ValueError, match="https or loopback"):
        second_review.ask(cfg, {}, "diff")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))

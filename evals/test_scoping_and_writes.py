"""Scoping an agent's tasks and writes, and not reading an image mid-write.

Asked for, from a team running a-team on its own host:
- an agent kept to some task projects (roster allowed_task_projects), with
  subtasks in their parent's project, so scoping a project scopes its tree;
- a write allow-list (write_roots) beside refuse_paths, and refuse_paths
  checked on a shell command's words, as the shell splits them;
- a file tool refused an image that has no end marker yet: a render still
  writing it.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateam import mcp_tasks, policy, tasks  # noqa: E402

PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"


@pytest.fixture
def team(tmp_path, monkeypatch):
    td = tmp_path / "team"
    (td / "inbox").mkdir(parents=True)
    work, answers = tmp_path / "work", tmp_path / "answers"
    work.mkdir()
    answers.mkdir()
    (td / "roster.json").write_text(json.dumps({
        "policy": {"refuse_paths": [str(answers)]},
        "agents": [
            {"name": "homer", "role": "home projects", "allowed_task_projects": ["home"],
             "write_roots": [str(work)]},
            {"name": "webby", "role": "web", "allowed_task_projects": ["web-*", "ops"]},
            {"name": "lead", "role": "coordinates"}]}))
    monkeypatch.setenv("ATEAM_TEAM_DIR", str(td))
    monkeypatch.setenv("ATEAM_DURABLE_DIR", str(tmp_path / "durable"))
    monkeypatch.delenv("ATEAM_AGENT", raising=False)
    return td, work, answers


def check(agent, tool, args, td):
    return policy.check_tool_policy(agent, tool, args, team_dir=td)


# --- task projects ------------------------------------------------------------

def test_an_agent_works_only_in_its_task_projects(team):
    td, _, _ = team
    out = mcp_tasks._create_task("homer", td, {"title": "fix the gate", "project": "work"})
    assert out.startswith("[refused:") and "allowed_task_projects" in out
    out = mcp_tasks._create_task("homer", td, {"title": "fix the gate"})
    assert "[task created" in out, "its one project is the default"
    mine = tasks.list_tasks(td)[0]
    assert mine["project"] == "home"
    theirs = tasks.create(td, "web-shop", "checkout page", created_by="lead")
    for refused in (mcp_tasks._claim_task("homer", td, {"task_id": theirs["id"]}),
                    mcp_tasks._update_task("homer", td, {"task_id": theirs["id"], "note": "x"}),
                    mcp_tasks._complete_task("homer", td, {"task_id": theirs["id"], "note": "x"}),
                    mcp_tasks._update_task("homer", td, {"task_id": mine["id"], "project": "web-shop"})):
        assert refused.startswith("[refused:"), refused
    assert "[claimed]" in mcp_tasks._claim_task("webby", td, {"task_id": theirs["id"]})
    listed = mcp_tasks._list_tasks(td, {}, "homer")
    assert "fix the gate" in listed and "checkout page" not in listed
    assert "checkout page" in mcp_tasks._list_tasks(td, {}, "lead"), "an unscoped agent sees all"


def test_a_subtask_is_in_its_parents_project(team):
    td, _, _ = team
    top = tasks.create(td, "home", "kitchen", created_by="lead")
    sub = tasks.create(td, "", "tiles", parent=top["id"], created_by="lead")
    assert sub["project"] == "home"
    with pytest.raises(tasks.TaskError, match="parent's project"):
        tasks.create(td, "web-shop", "tiles", parent=top["id"], created_by="lead")
    other = tasks.create(td, "ops", "loose end", created_by="lead")
    moved = tasks.update(td, other["id"], "lead", parent=top["id"])
    assert moved["project"] == "home", "moved under a task, it takes that task's project"
    tasks.update(td, top["id"], "lead", project="ops")
    assert {tasks.get(td, t)["project"] for t in (top["id"], sub["id"], other["id"])} == {"ops"}
    out = mcp_tasks._create_task("homer", td, {"title": "grout", "parent": top["id"]})
    assert out.startswith("[refused:"), "a subtask of out-of-scope work is out of scope"


def test_a_pattern_names_several_projects(team):
    td, _, _ = team
    assert "[task created" in mcp_tasks._create_task("webby", td, {"title": "a", "project": "web-blog"})
    out = mcp_tasks._create_task("webby", td, {"title": "b"})
    assert out.startswith("[refused:"), "with several projects there is no default"


# --- write_roots ----------------------------------------------------------------

def test_writes_only_under_write_roots_and_reads_anywhere(team, tmp_path):
    td, work, _ = team
    elsewhere = tmp_path / "elsewhere" / "notes.md"
    assert check("homer", "Write", {"file_path": str(work / "a.py")}, td) is None
    refusal = check("homer", "Write", {"file_path": str(elsewhere)}, td)
    assert refusal and "write_roots" in refusal
    assert check("homer", "Read", {"file_path": str(elsewhere)}, td) is None
    assert check("homer", "Bash", {"command": f"echo hi > '{elsewhere}'"}, td)
    assert check("homer", "Bash", {"command": f"cp {work}/a.py {elsewhere.parent}/"}, td)
    assert check("homer", "Bash", {"command": f"sed -i s/a/b/ {elsewhere}"}, td)
    assert check("homer", "Bash", {"command": f"cat {elsewhere} > {work}/copy.md 2>/dev/null"}, td) is None
    assert check("homer", "Bash", {"command": "echo hi > out.txt", "cwd": str(work)}, td) is None
    assert check("lead", "Write", {"file_path": str(elsewhere)}, td) is None, "no roots, no limit"


# --- refuse_paths, word by word --------------------------------------------------

def test_a_command_run_inside_a_refused_directory_is_refused(team):
    td, work, answers = team
    (answers / "key.json").write_text("{}")
    assert check("lead", "Bash", {"command": "cat key.json", "cwd": str(answers)}, td)
    assert check("lead", "Bash", {"command": "cat ../answers/key.json", "cwd": str(work)}, td)
    assert check("lead", "Bash", {"command": f"python x.py --input={answers}/key.json"}, td)
    assert check("lead", "Bash", {"command": f"sh -c 'cat \"{answers}\"/key.json'"}, td)
    assert check("lead", "Bash", {"command": "cat notes.md", "cwd": str(work)}, td) is None
    assert check("lead", "Bash", {"command": "grep -r answers .", "cwd": str(work)}, td) is None


# --- images still being written ---------------------------------------------------

def test_an_image_without_its_end_marker_is_not_read(team, tmp_path):
    td, _, _ = team
    png = tmp_path / "plot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    refusal = check("lead", "Read", {"file_path": str(png)}, td)
    assert refusal and "still being written" in refusal
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200 + PNG_END)
    assert check("lead", "Read", {"file_path": str(png)}, td) is None
    jpg = tmp_path / "photo.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x11" * 300)
    assert check("lead", "view_file", {"AbsolutePath": str(jpg)}, td)
    old = time.time() - 3600
    os.utime(jpg, (old, old))
    assert check("lead", "Read", {"file_path": str(jpg)}, td) is None, \
        "a JPEG nothing is writing may carry data past its end (a motion photo)"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x11" * 300 + b"\xff\xd9")
    assert check("lead", "Read", {"file_path": str(jpg)}, td) is None
    assert check("lead", "Write", {"file_path": str(png.with_name("new.png"))}, td) is None


def test_the_image_check_can_be_turned_off(team, tmp_path):
    td, _, _ = team
    doc = json.loads((td / "roster.json").read_text())
    doc["policy"]["refuse_partial_images"] = False
    (td / "roster.json").write_text(json.dumps(doc))
    png = tmp_path / "plot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    assert check("lead", "Read", {"file_path": str(png)}, td) is None


@pytest.mark.parametrize("name,data,writing", [
    ("a.gif", b"GIF89a" + b"\x00" * 40, True),
    ("a.gif", b"GIF89a" + b"\x00" * 40 + b";", False),
    ("a.webp", b"RIFF" + (100).to_bytes(4, "little") + b"WEBP" + b"\x00" * 20, True),
    ("a.webp", b"RIFF" + (100).to_bytes(4, "little") + b"WEBP" + b"\x00" * 96, False),
    ("a.png", b"", True),
    ("notes.txt", b"", False),
    ("a.png", b"not a png at all, just named one", False),
])
def test_end_markers(tmp_path, name, data, writing):
    p = tmp_path / name
    p.write_bytes(data)
    assert policy.image_still_writing(p) is writing


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))

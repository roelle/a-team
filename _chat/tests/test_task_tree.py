"""The task tree over HTTP: parents, levels, roll-ups, archive, and turning
project labels into parent tasks."""
from test_api import call, server, team  # noqa: F401  (fixtures)


def task(port, token, **kw):
    _, t = call(port, "POST", "/api/task", {"team": "demo", "action": "create", **kw}, token=token)
    return t


def test_tasks_carry_their_place_in_the_tree(server):
    app, port, *_ = server
    p = task(port, app.token, title="Billing revamp", level=1, priority=1, auto_close=True)
    k = task(port, app.token, title="dedupe", parent=p["id"])
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    rows = {t["id"]: t for t in st["tasks"]}
    assert st["task_levels"][:2] == ["task", "project"]
    assert rows[p["id"]]["node"]["children"] == [k["id"]] and rows[p["id"]]["priority"] == 1
    assert rows[k["id"]]["parent"] == p["id"] and rows[k["id"]]["node"]["path"] == ["Billing revamp"]
    assert rows[p["id"]]["auto_close"] is True and rows[p["id"]]["node"]["level"] == 1


def test_a_task_moves_and_a_branch_archives(server):
    app, port, *_ = server
    a, b = task(port, app.token, title="A"), task(port, app.token, title="B")
    call(port, "POST", "/api/task", {"team": "demo", "action": "update", "id": b["id"], "parent": a["id"]},
         token=app.token)
    status, err = call(port, "POST", "/api/task", {"team": "demo", "action": "update", "id": a["id"],
                                                   "parent": b["id"]}, token=app.token)
    assert status == 400 and "inside itself" in err["error"]
    call(port, "POST", "/api/task", {"team": "demo", "action": "update", "id": a["id"], "archived": True},
         token=app.token)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    rows = {t["id"]: t for t in st["tasks"]}
    assert rows[b["id"]]["node"]["archived_by"] == a["id"]


def test_project_labels_become_parent_tasks(server):
    app, port, *_ = server
    for i in range(3):
        task(port, app.token, title=f"t{i}", project="ops")
    _, r = call(port, "POST", "/api/task", {"team": "demo", "action": "migrate_labels", "dry_run": True},
                token=app.token)
    assert r["plan"] == [{"project": "ops", "tasks": 3, "closed": False, "parent": None}]
    call(port, "POST", "/api/task", {"team": "demo", "action": "migrate_labels"}, token=app.token)
    _, st = call(port, "GET", "/api/state?team=demo", token=app.token)
    ops = next(t for t in st["tasks"] if t["title"] == "ops")
    assert ops["node"]["level"] == 1 and len(ops["node"]["children"]) == 3

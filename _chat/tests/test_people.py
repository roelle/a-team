"""Links of their own for other people, and what those links cannot reach.

Asked for as multiple humans. A person here is a name on a short list and on
what they post. The tests lean on the boundary: the operator's link reads the
operator's home directory and runs the team, and a link handed to a friend must
do neither.
"""
import http.client
import json
import stat
from pathlib import Path

import pytest

from agychat import people
from test_api import call, server, team  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def book(tmp_path, monkeypatch):
    monkeypatch.setenv("AGYCHAT_PEOPLE", str(tmp_path / "config" / "people.json"))


@pytest.fixture
def alice():
    return people.add("alice")


def test_the_token_is_not_kept(alice):
    text = people.path().read_text()
    assert alice not in text and people.who(alice) == "alice"
    assert stat.S_IMODE(people.path().stat().st_mode) == 0o600


@pytest.mark.parametrize("bad", ["", "a b", "../x", "user", "Supervisor", "all"])
def test_bad_names_are_refused(bad):
    with pytest.raises(people.PeopleError):
        people.add(bad)


def test_one_name_once(alice):
    with pytest.raises(people.PeopleError, match="already"):
        people.add("Alice")


def test_a_wrong_or_empty_token_is_nobody(alice):
    assert people.who("") is None and people.who("nope") is None


def test_they_post_under_their_own_name(server, alice):
    app, port, td, _ = server
    status, _ = call(port, "POST", "/api/post?team=demo", {"to": "coder", "content": "hi"}, alice)
    assert status == 200
    row = [json.loads(x) for x in (td / "bus.jsonl").read_text().splitlines()][-1]
    assert row["from"] == "user" and row["author"] == "alice"


def test_the_state_says_who_they_are(server, alice):
    port = server[1]
    state = call(port, "GET", "/api/state?team=demo", token=alice)[1]
    assert state["author"] == "alice" and state["member"] is True


def test_the_operator_is_not_left_as_the_last_person_on_the_thread(server, alice):
    app, port, *_ = server
    assert call(port, "GET", "/api/state?team=demo", token=alice)[1]["author"] == "alice"
    assert call(port, "GET", "/api/state?team=demo", token=app.token)[1]["member"] is False
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    for token, member in ((alice, True), (app.token, False), (alice, True)):
        c.request("GET", "/api/state?team=demo",
                  headers={"Host": f"127.0.0.1:{port}", "X-Token": token})
        assert json.loads(c.getresponse().read())["member"] is member


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/admin?team=demo", None), ("GET", "/api/versions", None),
    ("GET", "/api/setup", None), ("GET", "/api/memories?team=demo&agent=coder", None),
    ("GET", "/api/norm_candidates?team=demo", None),
    ("POST", "/api/update", {"team": "demo"}), ("POST", "/api/agent", {"team": "demo"}),
    ("POST", "/api/norms", {"team": "demo", "text": "x"}),
    ("POST", "/api/memory", {"team": "demo"}),
    ("POST", "/api/supervisor", {"team": "demo", "action": "stop"}),
    ("POST", "/api/model", {"team": "demo"}), ("POST", "/api/channel", {"team": "demo"}),
    ("POST", "/api/new_team", {"name": "x", "runtime": "agy"}),
    ("POST", "/api/avatar", {"team": "demo"}),
    ("POST", "/api/task", {"team": "demo", "action": "waive", "id": "x"}),
    ("POST", "/api/task", {"team": "demo", "action": "migrate_labels"})])
def test_what_is_the_operators_stays_the_operators(server, alice, method, path, body):
    status, doc = call(server[1], method, path, body, alice)
    assert status == 403, doc


def test_a_new_endpoint_starts_closed(server, alice):
    assert call(server[1], "GET", "/api/anything-new", token=alice)[0] == 403
    assert call(server[1], "POST", "/api/anything-new", {}, alice)[0] == 403


def test_the_board_is_theirs_to_use(server, alice):
    port = server[1]
    status, t = call(port, "POST", "/api/task", {"team": "demo", "action": "create",
                                                 "title": "from alice"}, alice)
    assert status == 200 and t["title"] == "from alice"
    status, _ = call(port, "POST", "/api/task", {"team": "demo", "action": "update",
                                                 "id": t["id"], "note": "x"}, alice)
    assert status == 200


def test_they_read_the_team_but_not_the_operators_home(server, alice, tmp_path, monkeypatch):
    app, port, td, ws = server
    monkeypatch.setenv("HOME", str(tmp_path))
    secret = tmp_path / "taxes.txt"
    secret.write_text("private")
    from urllib.parse import quote
    q = f"team=demo&path={quote(str(secret))}"
    assert call(port, "GET", f"/api/file?{q}", token=app.token)[0] == 200
    assert call(port, "GET", f"/api/file?{q}", token=alice)[0] == 404
    assert "error" in call(port, "GET", f"/api/artifact?{q}", token=alice)[1]
    mine = quote(str(ws / "report.md"))
    assert call(port, "GET", f"/api/file?team=demo&path={mine}", token=alice)[0] == 200


def test_removing_someone_ends_their_link_at_once(server, alice):
    port = server[1]
    assert call(port, "GET", "/api/teams", token=alice)[0] == 200
    assert people.remove("alice")
    assert call(port, "GET", "/api/teams", token=alice)[0] == 401
    assert people.remove("alice") is False


def test_a_link_logs_them_in_and_the_cookie_works(server, alice):
    port = server[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", f"/?token={alice}", headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    r.read()
    cookie = r.getheader("Set-Cookie")
    assert r.status == 200 and alice in cookie and "HttpOnly" in cookie
    c.request("GET", "/api/state?team=demo",
              headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie.split(";")[0]})
    assert json.loads(c.getresponse().read())["author"] == "alice"


def test_a_token_that_is_not_ascii_is_refused_not_an_error(server):
    assert call(server[1], "GET", "/api/teams", token="café".encode().decode("latin-1"))[0] == 401


def test_the_command_line(capsys):
    people.main(["add", "bob", "--url", "http://host:9/"])
    out = capsys.readouterr().out
    assert out.startswith("http://host:9/?token=") and "shown once" in out
    assert "run commands as you" in out
    people.main(["list"])
    assert capsys.readouterr().out.strip() == "bob"
    people.main(["remove", "bob"])
    assert people.names() == []

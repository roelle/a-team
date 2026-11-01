"""The people who share this app, each with a link of their own.

    agy-chat people add alice --url http://host:8765
    agy-chat people list
    agy-chat people remove alice

A person is who wrote a message and a name on a short list; there is no account
behind it. What they post carries their name, so agents can answer them. They
can use chat and the board. They cannot use Admin, start or stop a team, or read
a file outside the team and its workspaces, which the operator's link can.

Only a hash of each link's token is kept. Removing a person ends their link at
once, because the list is read on every request.

Anyone on this list can tell the team's agents to do things, and those agents
run commands as the operator. Add people accordingly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
WARNING = ("Whoever has this link can read what the team says and tell its agents "
           "what to do, and those agents run commands as you.")


class PeopleError(Exception):
    """Something a person can fix; the message says what."""


def path() -> Path:
    raw = os.environ.get("AGYCHAT_PEOPLE")
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "agy-chat" / "people.json"


def _load() -> dict:
    try:
        doc = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save(doc: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, p)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def add(name: str) -> str:
    """Add a person; their token, which exists nowhere else afterwards."""
    if not _NAME.match(name or ""):
        raise PeopleError("a name is letters, digits, ., - and _, up to 40 characters")
    if name.lower() in ("user", "supervisor", "here", "all", "everyone", "channel"):
        raise PeopleError(f"{name} is a reserved name")
    doc = _load()
    if name.lower() in (n.lower() for n in doc):
        raise PeopleError(f"{name} is already here; remove them first for a new link")
    token = secrets.token_urlsafe(24)
    doc[name] = {"token_sha256": _hash(token), "added": time.strftime("%Y-%m-%d")}
    _save(doc)
    return token


def remove(name: str) -> bool:
    doc = _load()
    if doc.pop(name, None) is None:
        return False
    _save(doc)
    return True


def names() -> list[str]:
    return sorted(_load())


def who(token: str) -> str | None:
    """The person this token belongs to."""
    if not token:
        return None
    wanted = _hash(token)
    found = None
    for name, entry in _load().items():
        if secrets.compare_digest(str(entry.get("token_sha256", "")), wanted):
            found = name
    return found


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="agy-chat people", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    new = sub.add_parser("add", help="add someone and print their link, once")
    new.add_argument("name")
    new.add_argument("--url", default="http://127.0.0.1:8765",
                     help="the address they reach this app at")
    sub.add_parser("list", help="who is here")
    gone = sub.add_parser("remove", help="end someone's link")
    gone.add_argument("name")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        print("\n".join(names()) or "nobody yet")
    elif a.cmd == "remove":
        if not remove(a.name):
            sys.exit(f"nobody called {a.name}")
        print(f"removed {a.name}")
    else:
        try:
            token = add(a.name)
        except PeopleError as e:
            sys.exit(str(e))
        print(f"{a.url.rstrip('/')}/?token={token}\n\nThis is shown once. {WARNING}")
    return 0

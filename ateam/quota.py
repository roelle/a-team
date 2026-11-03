"""How much of a model's quota is left, and what it costs: what each runner
knows, kept where the app, list_models and a downstream host can read it.

A model listing is [{"id", "label"}]; these optional fields are added where
something is known, and a runner or host that sets one itself is not
overwritten:

    remaining   0-1, the fraction left of the tightest limit that applies
    resets_at   when that limit resets, ISO 8601 with its offset
    window      which limit that is ("five_hour", "seven_day", "primary"...)
    status      "allowed", "allowed_warning" or "rejected", when the runtime says
    credits     a prepaid balance, as the runtime reports it (a string)
    cost        {"input", "output"}: USD per million tokens, from
                config.PRICING. On a subscription it is what the tokens would
                cost on the API, not what is billed.

Runners report what they see during turns with note(): the Claude runner its
CLI's rate-limit events (a subscription's five-hour and weekly limits), the
Codex runner the app-server's account limits and credits. A runner that sees
nothing -- the agy CLI, an API key with no limit headers -- reports nothing,
and only `cost` is filled where the model is priced. A host runner can call
Runner.note_quota from its adapter, or put the fields on its config's
"models" entries.

Kept in <team_dir>/quota.json: per account (the runner's label), each limit
as last seen and the models seen running on it. A limit whose reset time has
passed is dropped: it has reset, and how much is left is unknown again.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

FILE = "quota.json"
#: Below this fraction left, the app marks an agent's model as low.
LOW = 0.2


def _path(team_dir) -> Path:
    return Path(team_dir) / FILE


def _load(team_dir) -> dict:
    try:
        doc = json.loads(_path(team_dir).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def iso(t) -> str | None:
    """A reset time as ISO 8601, from epoch seconds (or milliseconds) or a
    string already in that form."""
    if t is None or t == "":
        return None
    if isinstance(t, str):
        try:
            t = float(t)
        except ValueError:
            return t
    t = float(t)
    if t > 1e11:                    # milliseconds
        t /= 1000
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _epoch(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def note(team_dir, account: str, model: str | None, window: str, *,
         remaining: float | None = None, resets_at=None, status: str | None = None,
         applies: str | None = None, credits: str | None = None) -> None:
    """Record a limit as seen now. `account`: whose quota (the runner's
    label). `model`: the model that was running, so the limit is shown on
    it. `applies`: the limit is only for models whose id contains this
    ("opus"); None, for every model on the account."""
    if not team_dir or not account:
        return
    from . import filelock
    path = _path(team_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with filelock.locked(path):
        doc = _load(team_dir)
        acct = doc.setdefault(account, {"models": [], "windows": {}})
        if model and model not in acct["models"]:
            acct["models"].append(model)
        if window:
            w = {k: v for k, v in {
                "remaining": None if remaining is None else round(max(0.0, min(1.0, float(remaining))), 4),
                "resets_at": iso(resets_at), "status": status, "applies": applies,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S")}.items() if v is not None}
            acct["windows"][window] = {**acct["windows"].get(window, {}), **w}
        if credits is not None:
            acct["credits"] = str(credits)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def for_model(team_dir, model: str | None, account: str | None = None) -> dict:
    """What is known of `model`'s quota: the tightest live limit of any
    account it was seen on (or of `account`, which lists it), with
    "remaining", "resets_at", "window", "status", "credits" as known.
    {} when nothing is."""
    if not model or not team_dir:
        return {}
    now = time.time()
    best, credits = None, None
    for name, acct in _load(team_dir).items():
        if not isinstance(acct, dict):
            continue
        if name != account and model not in (acct.get("models") or []):
            continue
        credits = acct.get("credits", credits)
        for wname, w in (acct.get("windows") or {}).items():
            if w.get("applies") and w["applies"] not in model:
                continue
            end = _epoch(w.get("resets_at"))
            if end is not None and end <= now:
                continue
            left = w.get("remaining")
            if left is None and w.get("status") == "rejected":
                left = 0.0
            if left is None and w.get("status") != "allowed_warning":
                continue
            key = 1.0 if left is None else left
            if best is None or key < best[0]:
                best = (key, {"remaining": left, "resets_at": w.get("resets_at"),
                              "window": wname, "status": w.get("status")})
    out = {k: v for k, v in (best[1] if best else {}).items() if v is not None}
    if credits is not None:
        out["credits"] = credits
    return out


def cost(model: str | None) -> dict | None:
    from . import config as cfg
    price = cfg.PRICING.get(model or "")
    return {"input": price[0], "output": price[1]} if price else None


def annotate(models: list[dict] | None, team_dir=None, account: str | None = None) -> list[dict] | None:
    """`models` with what is known of each one's quota and cost added,
    leaving whatever a runner already set."""
    if models is None:
        return None
    out = []
    for m in models:
        extra = dict(for_model(team_dir, m.get("id"), account)) if team_dir else {}
        c = cost(m.get("id"))
        if c:
            extra["cost"] = c
        out.append({**extra, **m})
    return out


def low(q: dict, threshold: float = LOW) -> bool:
    """Is this quota low: under `threshold` left, or refused?"""
    left = q.get("remaining")
    return q.get("status") == "rejected" or (left is not None and left < threshold)

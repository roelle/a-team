"""A second opinion from another vendor's model on approved code.

A reviewer on the same model as the author shares its blind spots. So, when
a team asks for it, every approved code review is followed by one call to a
model from somewhere else, given the change and the review, asking for
defects. It returns findings only: it cannot block, approve or reopen, and
what it says goes to the reviewer who approved, to weigh against the code.

    "second_review": {"model": "gpt-6-astra",
                      "paths": ["src/auth/*", "*migrations*"]}

in roster.json, or under "policy". Optional: "paths", globs a change must
touch to be sent, for only the risky ones; "provider": {"base_url",
"env_key"} for an endpoint other than OpenAI's (any that speaks the Responses
API); "max_chars" for how much of the change is sent. The key comes from the
environment ("OPENAI_API_KEY" by default) and nowhere else.

The change is what git says moved in each workspace since the episode began,
committed or not, plus new files. Without git there is no change to show, and
nothing is sent.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {"model": "gpt-6-astra", "max_chars": 60_000, "paths": [],
            "provider": {"base_url": "https://api.openai.com/v1", "env_key": "OPENAI_API_KEY"}}
TIMEOUT_S = 180
NEW_FILE_CHARS = 8_000

PROMPT = """You are a second reviewer from outside this team. Another model approved \
the change below. Find what it missed.

Report only defects you can point to in the change: wrong behaviour, a case \
the code mishandles, a test that does not test what it claims, a security \
problem. For each, give the file and line, what goes wrong and the input or \
state that shows it. Leave out style, naming and suggestions. If you find \
nothing, say "No findings." and stop. You cannot approve or block anything; \
the team decides.

The review that approved it:
{review}

The change:
{diff}"""


def config(team_dir) -> dict | None:
    """The team's settings, or None when it has not asked for this."""
    try:
        doc = json.loads((Path(team_dir) / "roster.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cfg = doc.get("second_review", (doc.get("policy") or {}).get("second_review"))
    if not cfg or (isinstance(cfg, dict) and cfg.get("enabled") is False):
        return None
    cfg = cfg if isinstance(cfg, dict) else {}
    return {**DEFAULTS, **cfg, "provider": {**DEFAULTS["provider"], **(cfg.get("provider") or {})}}


def _git(root, *args) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def bases(workspaces) -> dict[str, str]:
    """{repository root: HEAD} for each workspace in git, at the episode's start."""
    out = {}
    for w in workspaces or []:
        code, root = _git(w, "rev-parse", "--show-toplevel")
        if code or not root.strip() or root.strip() in out:
            continue
        code, head = _git(root.strip(), "rev-parse", "HEAD")
        if not code and head.strip():
            out[root.strip()] = head.strip()
    return out


def change(base: dict[str, str]) -> tuple[str, list[str]]:
    """(the diff since `base`, with new files, as text; the paths it touches)."""
    parts, paths = [], []
    for root, head in base.items():
        code, names = _git(root, "diff", "--name-only", head)
        _, new = _git(root, "ls-files", "--others", "--exclude-standard")
        touched = [n for n in names.splitlines() + new.splitlines() if n.strip()]
        if not touched:
            continue
        paths += touched
        _, diff = _git(root, "diff", head)
        parts.append(f"## {root}\n{diff}")
        for n in new.splitlines():
            try:
                text = (Path(root) / n).read_text(encoding="utf-8")[:NEW_FILE_CHARS]
            except (OSError, UnicodeDecodeError):
                continue
            parts.append(f"## {root}/{n} (new file)\n{text}")
    return "\n\n".join(parts), paths


def wanted(cfg: dict, paths: list[str]) -> bool:
    globs = cfg.get("paths") or []
    return bool(paths) and (not globs or any(fnmatch.fnmatch(p, g) for p in paths for g in globs))


def ask(cfg: dict, review: dict, diff: str) -> dict:
    """{"text", "input_tokens", "output_tokens"} from one Responses API call.
    Raises on anything but an answer."""
    prov = cfg["provider"]
    url = str(prov.get("base_url") or "").rstrip("/")
    host = urlsplit(url).hostname or ""
    if not url.startswith("https://") and host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"second_review base_url must be https or loopback, got {url!r}")
    key = os.environ.get(str(prov.get("env_key") or ""))
    if not key:
        raise ValueError(f"{prov.get('env_key')} is not set in the supervisor's environment")
    cap = int(cfg.get("max_chars") or DEFAULTS["max_chars"])
    shown = diff if len(diff) <= cap else diff[:cap] + f"\n[... {len(diff) - cap} more characters left out]"
    about = {k: review.get(k) for k in ("what", "author", "reviewer", "findings", "proof_file")}
    body = {"model": cfg["model"], "store": False,
            "input": PROMPT.format(review=json.dumps(about, indent=1), diff=shown)}
    req = urllib.request.Request(f"{url}/responses", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            doc = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None
    text = "".join(c.get("text", "") for item in doc.get("output") or []
                   if item.get("type") == "message" for c in item.get("content") or [])
    usage = doc.get("usage") or {}
    return {"text": text.strip(), "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens")}


def note(cfg: dict, review: dict, text: str) -> str:
    """The message the reviewer gets."""
    return (f"A second review of \"{str(review.get('what') or '')[:120]}\", which you approved, "
            f"by {cfg['model']} from outside the team. It is findings only, not a verdict: "
            f"check each against the code, and reopen the work if one holds.\n\n{text}")

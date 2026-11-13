"""Triggers judged by what happened after, and learned from it.

## Why this file exists

Asked for: "Yes on suggest. Nothing says that the memory needs to match the
command or words in the prompt. The goal is to pull information that is
useful from disk. I don't have religion about the recall algorithm. I want
it judged post facto. It doesn't have to do the right thing right now: it's
going to get called over and over again, with similar expectations of
results. So that starts to look like a control algorithm, something with
feedback. We just want the memories that are returned to be the most
useful: to be converging over time to the best memories that are
available."

So which memory comes up before which call is not settled by what a memory
says it is about, or by the words the two share. It is a record, kept per
agent, of each (memory, trigger) pair and how showing it went, and it is
corrected after every turn from what the turn did:

- **A call went wrong and was made again**, with nothing shown. Was there
  a memory that would have made the first try go right? The agent opening
  one before the try that worked says so, at no cost. Otherwise a judge
  (the model recall uses) is given the call, what came back, what worked
  and the agent's memories, and names the memory and the trigger. That
  memory now comes up before such calls: a learned trigger nobody wrote.
- **A memory was shown** before a call. Did it bear on what the agent did
  next? The judge says; "no" counts against that trigger, whoever wrote it.
- **A call went wrong with its memory out of sight** (shown earlier in a
  long conversation, and not yet due again): it is shown again at the next
  such call, without waiting for the context to grow or the hours to pass.

A trigger is shown while its recent record says it helps at least
`show_above` of the time (0.3: a held call costs one step, a call made
wrong costs several). Below that it stops: a learned one is retired, a
written one is muted, and a memory with no trigger left goes back to
wake recall. The same evidence brings either back. The record forgets:
each new verdict keeps `decay` (0.9) of the old counts, so it is a running
average over about the last ten, between every call ever made and only
the last one.

Where no memory would have helped, the lesson is missing. Those calls are
put to the agent when it next writes down what it learned, and when it
reviews its memories: the first try, what came back and what worked, to
be saved with a trigger so that the next first try goes right. An agent
that saves a memory with `triggers` set has decided, and what was learned
for that memory starts over.

Without a judge (recall off, and no "judge" under "memory_triggers") only
what can be read off the record counts: a memory opened after a failed
call, and a held call that came back changed and went through. Nothing is
then ever muted.

State: <team_dir>/triggers/<agent>.learned.json. `python -m
ateam.trigger_learn [agent]` prints it.
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

from . import filelock, triggers

#: (helped, noise) a trigger starts with before any evidence: one the agent
#: wrote is trusted until five straight verdicts say otherwise; a learned
#: one exists only because of evidence, and two against it retire it.
PRIOR = {True: (2.0, 1.0), False: (0.0, 0.5)}
#: The team's own reads, which are never a call that "went wrong".
QUIET = {"read_memory", "memory_index", "review_memory", "check_inbox"}
#: A file changed between two tries: the code was fixed and run again,
#: which is ordinary work and not a mistake about the call.
EDITS = re.compile(r"edit|write|patch|replace|create_file", re.I)
#: The agent looking up how to make the call.
LOOKUP = re.compile(r"\s--help\b|\s--usage\b|\s-h\s*$|^\S+\s+help\b|\shelp\s*$")
_EXIT = re.compile(r"(?i)\b(exit(ed)?( with)?( code| status)?|code|status)\s*:?\s*-?\d+\b")
TAIL_BYTES = 16 * 1024 * 1024
FOLLOWING = 12          # calls after a showing that the judge is given
CANDIDATES = 30         # memories put to the judge for one failed call
KEEP = 60               # trouble and news entries kept

HELPER_PROMPT = """An AI agent on a team {lead}:

<first_try>
{call}
</first_try>

<what_came_back>
{error}
</what_came_back>

<then>
{then}
</then>

Below are lessons the agent had already saved in its own memory. None was in front of it at the time. Is one of them the lesson that, read just before the first try, would have made it go right the first time, with nothing to look up? Choose it only if it says what was missing here: the flag, the order of the steps, the thing to check first. A lesson that is merely about the same tool does not count, and most calls that go wrong have no such lesson: answer null unless one plainly does.

If you choose one, also say which calls it should come up before from now on, written as a tool and what it is called on: "Bash(kubectl apply)" is that command with any arguments, "Bash(helm *)" is any helm command, "Edit(*.ipynb)" is a file tool on matching paths, "send_to_teammate(user)" is a message to that teammate. It must cover the first try above and the next call that would make the same mistake, and reach no further than the lesson does.

{candidates}

Return JSON {{"memory": "<id>" or null, "trigger": "<Tool(pattern)>" or null}}"""

BORE_PROMPT = """An AI agent on a team made this call:

<call>
{call}
</call>

At that point it was shown this memory of its own, because the memory is set to come up before calls like it (the call was held while it read the memory, or the memory arrived straight after):

<memory>
{memory}
</memory>

What the agent did next -- its calls and whether each went through, possibly cut short:

<actions>
{actions}
</actions>

Decide whether the memory bore on this call. Weigh what the memory says against what the call was and what the agent then did; shared words prove nothing either way. Verdicts:
- "used": the memory is about calls like this one, and what the agent did next follows it: it changed the call, or took a step the memory asks for, or the call already did what the memory asks.
- "ignored": the memory is about calls like this one, and the agent went against it with no visible reason.
- "overruled": the agent had a visible reason not to follow it here.
- "na": the memory is not about this call. Nothing in it would change how a careful agent makes it; showing it cost a step and gave nothing.
Return JSON {{"verdict": "used" | "ignored" | "overruled" | "na", "evidence": "a short phrase"}}"""


# --- the record ---------------------------------------------------------------------

def _path(team_dir, agent: str) -> Path:
    return Path(team_dir) / triggers.DIR / f"{agent}.learned.json"


def _state(path: Path) -> dict:
    doc = triggers._load(path)
    for k, empty in (("scores", {}), ("trouble", []), ("news", [])):
        if not isinstance(doc.get(k), type(empty)):
            doc[k] = empty
    return doc


def _stamp(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))


def score(rec: dict) -> float:
    """How often showing this has helped, lately: 0 to 1."""
    ph, pn = PRIOR[bool(rec.get("written"))]
    h, n = float(rec.get("helped") or 0.0), float(rec.get("noise") or 0.0)
    return (h + ph) / (h + n + ph + pn)


def standing(team_dir, agent: str) -> tuple[dict[str, list[str]], set[tuple[str, str]]]:
    """({memory: [triggers learned for it]}, {(memory, trigger) written and
    muted}) as the record stands. Never raises."""
    try:
        cfg = triggers.config(team_dir)
        if cfg is None or not cfg.get("learn"):
            return {}, set()
        learned: dict[str, list[str]] = {}
        muted: set[tuple[str, str]] = set()
        for rec in _state(_path(team_dir, agent))["scores"].values():
            live = score(rec) >= cfg["show_above"]
            if rec.get("written"):
                if not live:
                    muted.add((rec["memory"], rec["trigger"]))
            elif live:
                learned.setdefault(rec["memory"], []).append(rec["trigger"])
        return learned, muted
    except Exception:
        return {}, set()


def _credit(state: dict, cfg: dict, memory: str, trigger: str, written: bool,
            helped: float, noise: float, why: str, now: float | None = None) -> str:
    """One verdict on a trigger. Returns what it changed: "learned" (now
    shown), "back" (a written one shown again), "muted", or ""."""
    key = f"{memory}\t{trigger}"
    rec = state["scores"].get(key)
    new = rec is None
    if new:
        rec = state["scores"][key] = {"memory": memory, "trigger": trigger,
                                      "helped": 0.0, "noise": 0.0, "n": 0}
    rec["written"] = bool(written)
    was = score(rec) >= cfg["show_above"] if (written or not new) else False
    rec["helped"] = round(rec["helped"] * cfg["decay"] + helped, 4)
    rec["noise"] = round(rec["noise"] * cfg["decay"] + noise, 4)
    rec["n"] += 1
    rec["ts"], rec["why"] = _stamp(now), why[:200]
    live = score(rec) >= cfg["show_above"]
    change = ""
    if live and not was:
        change = "back" if written else "learned"
    elif was and not live:
        change = "muted"
    if change:
        state["news"] = (state["news"] + [{
            "ts": rec["ts"], "kind": change, "memory": memory, "trigger": trigger,
            "why": rec["why"], "n": rec["n"]}])[-KEEP:]
    return change


def forget(team_dir, agent: str, memory: str) -> None:
    """The agent saved `memory` with its triggers set: it has decided, and
    what was learned about that memory's triggers starts over."""
    try:
        path = _path(team_dir, agent)
        if not path.exists():
            return
        with filelock.locked(path):
            state = _state(path)
            keep = {k: r for k, r in state["scores"].items() if r.get("memory") != memory}
            if len(keep) != len(state["scores"]):
                state["scores"] = keep
                triggers._save(path, state)
        triggers.forget_cache()
    except Exception:
        pass


# --- reading a turn -----------------------------------------------------------------

def turn_events(team_dir, agent: str, since: str) -> list[dict]:
    """The agent's calls, and what was shown before them, from `since`, in
    the order they happened."""
    path = Path(team_dir) / "events.jsonl"
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if f'"{agent}"' not in line or not ('"tool_call"' in line or '"triggered"' in line):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("agent") == agent and ev.get("event") in ("tool_call", "triggered") \
                and str(ev.get("ts") or "") >= since:
            out.append(ev)
    return out


def plain(events: list[dict]) -> list[dict]:
    """The events with each call a host made through its one generic
    MCP-call tool written as the call it made (triggers.unwrap), so it is
    shaped, matched and read like any other."""
    out = []
    for e in events:
        if e.get("event") == "tool_call":
            tool, args = triggers.unwrap(str(e.get("tool") or ""), _args(e))
            if tool != e.get("tool"):
                e = {**e, "tool": tool, "args": args}
        out.append(e)
    return out


def _args(ev: dict) -> dict:
    args = ev.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return args if isinstance(args, dict) else {}


def _text(tool: str, args: dict) -> str:
    if triggers.tool_name(tool).lower() in triggers.SHELL:
        return triggers.command(args)
    return json.dumps(args, default=str, sort_keys=True)


def _held(ev: dict) -> bool:
    """A call some hosts record as failed because it was held to show a
    memory: the showing, not a try."""
    return ev.get("event") == "tool_call" and "[held: this " in (
        str(ev.get("error") or "")[:200] + str(ev.get("result") or "")[:200])


def failed(ev: dict) -> bool:
    """A call that came back with an error that says something. A bare exit
    code (grep finding nothing, a test for a file) is an answer, not a
    mistake."""
    err = str(ev.get("error") or "")
    return len(re.sub(r"[^A-Za-z]", "", _EXIT.sub("", err))) >= 12


def episodes(events: list[dict]) -> list[dict]:
    """The calls that went wrong and were made again, differently, with no
    file changed in between; and the ones the agent looked up first."""
    calls = []
    for i, e in enumerate(events):
        if e.get("event") != "tool_call":
            continue
        args, tool = _args(e), str(e.get("tool") or "")
        text = _text(tool, args)
        calls.append({"i": i, "ev": e, "args": args, "tool": tool, "text": text,
                      "name": triggers.tool_name(tool),
                      "family": triggers.shape(tool, args, sub=False),
                      "failed": failed(e), "lookup": bool(LOOKUP.search(text))})
    out, taken = [], set()
    for k, c in enumerate(calls):
        if k in taken or not c["family"] or c["name"] in QUIET \
                or not (c["failed"] or c["lookup"]):
            continue
        chain = [k]
        for j in range(k + 1, len(calls)):
            d = calls[j]
            if d["family"] != c["family"]:
                if EDITS.search(d["name"]) and "todo" not in d["name"].lower():
                    break
                continue
            chain.append(j)
            if not d["ev"].get("error") and not d["lookup"]:
                break
        tries = [calls[j] for j in chain]
        real = [t for t in tries if not t["lookup"]]
        if len(tries) < 2 or not real or len({t["text"] for t in tries}) < 2:
            continue
        first, last = real[0], tries[-1]
        taken.update(chain)
        worked = last if not last["ev"].get("error") and not last["lookup"] else None
        went = first["ev"].get("error") or ""
        args = ({"command": first["text"][:400]}
                if first["name"].lower() in triggers.SHELL
                else {k2: (v if isinstance(v, (int, float, bool)) else str(v)[:200])
                      for k2, v in list(first["args"].items())[:8]})
        out.append({
            "shape": triggers.shape(first["tool"], first["args"]), "family": c["family"],
            "tool": first["tool"], "args": args, "call": first["text"][:400],
            "error": str(went or "(looked up how to make the call first)")[:600],
            "tries": len(tries), "looked_up": any(t["lookup"] for t in tries),
            "then": [(t["text"][:300], "looked it up" if t["lookup"] else
                      "went through" if not t["ev"].get("error") else
                      "failed: " + str(t["ev"].get("error"))[:200]) for t in tries[1:]],
            "worked": worked["text"][:400] if worked and worked is not first else "",
            "at": tries[0]["i"], "until": last["i"], "ts": str(tries[0]["ev"].get("ts") or "")})
    return out


def _memories(team_dir, agent: str) -> dict[str, dict]:
    from . import memory_log
    from .memory import extract_provenance
    try:
        store = memory_log.open_store(team_dir, agent)
    except Exception:
        return {}
    out = {}
    try:
        for e in store.index():
            raw = store.read(e.name) or ""
            _, _, body = extract_provenance(raw)
            out[e.name] = {"description": e.description, "body": triggers.strip(body).strip(),
                           "written": triggers.of(raw)}
    finally:
        store.close()
    return out


# --- the judge ----------------------------------------------------------------------

def judge_for(team_dir):
    """The model that answers afterwards, or None: "judge" under
    "memory_triggers" (settings as for "memory_recall", true, or false),
    else whatever recall is set up with."""
    try:
        from . import recall
        own = (triggers.config(team_dir) or {}).get("judge")
        if own is False:
            return None
        cfg = ({**recall.DEFAULTS, **(own if isinstance(own, dict) else {})} if own
               else recall.config(team_dir))
        backend = recall.backend_for(cfg) if cfg else None
        return backend if backend is not None and backend.available else None
    except Exception:
        return None


def _candidates(mems: dict[str, dict], ep: dict) -> list[str]:
    """The memories put to the judge: all of them while they fit; past that
    the ones nearest in words and a handful drawn at random, since the
    judge decides and a draw costs nothing."""
    names = list(mems)
    if len(names) <= CANDIDATES:
        return names
    from .recall import Index
    index = Index([{"id": n, "name": n.replace("-", " "), "description": m["description"],
                    "body": m["body"]} for n, m in mems.items()])
    near = [r["id"] for r in index.search(f"{ep['call']} {ep['error']} {ep['worked']}",
                                          CANDIDATES - 6)]
    rest = [n for n in names if n not in near]
    return near + random.sample(rest, min(len(rest), CANDIDATES - len(near)))


def _helper(judge, mems: dict[str, dict], ep: dict) -> tuple[str, str] | None:
    """(memory, trigger) that would have made this call go right, or None."""
    names = _candidates(mems, ep)
    text = "\n\n".join(f"<memory id=\"{n}\">\n{mems[n]['description']}\n"
                       f"{mems[n]['body'][:800]}\n</memory>" for n in names)
    then = "\n".join(f"{i}. {t}\n   -> {how}" for i, (t, how) in enumerate(ep["then"], 2))
    lead = ("made a call that went wrong, and had to make it again" if failed({"error": ep["error"]})
            else "had to look up how to make a call before it made it")
    ans = judge.ask(HELPER_PROMPT.format(lead=lead,
                                         call=f"{triggers.tool_name(ep['tool'])}: {ep['call']}",
                                         error=ep["error"], then=then or "(nothing more)",
                                         candidates=text))
    name = ans.get("memory")
    if name not in mems:
        return None
    trig = ep["shape"]
    try:
        asked = triggers.parse(ans.get("trigger"))[:1]
        m = triggers._ONE.match(asked[0]) if asked else None
        # Wide enough to match the call it was learned from; never the bare
        # tool, which would put it before every call there is.
        if m and (m.group(2) or "").strip() not in ("", "*") \
                and triggers.matches(asked[0], ep["tool"], ep["args"]):
            trig = asked[0]
    except triggers.TriggerError:
        pass
    return (name, trig) if trig else None


def _bore(judge, m: dict, ev: dict, after: list[dict]) -> tuple[float, float, str] | None:
    """(helped, noise, why) for one memory shown before one call."""
    acts = "\n".join(
        f"{triggers.tool_name(e.get('tool') or '')}: {_text(str(e.get('tool') or ''), _args(e))[:300]}"
        f" -> {'failed: ' + str(e['error'])[:160] if e.get('error') else 'went through'}"
        for e in after)
    ans = judge.ask(BORE_PROMPT.format(call=f"{ev.get('tool')}: {ev.get('call')}",
                                       memory=f"{m['description']}\n{m['body'][:1500]}",
                                       actions=acts[:6000]))
    verdict, evidence = ans.get("verdict") or "na", str(ans.get("evidence") or "")[:160]
    if verdict in ("used", "ignored"):          # ignored still bore on the call
        return 1.0, 0.0, f"{verdict}: {evidence}"
    if verdict == "na":
        return 0.0, 1.0, f"did not bear on the call: {evidence}"
    return None


# --- after a turn -------------------------------------------------------------------

def observe(team_dir, agent: str, events: list[dict], judge=None, record=None,
            now: float | None = None) -> list[str]:
    """Correct the record from one turn's events. Returns what changed, as
    lines for the log. `record(event, **fields)` puts each on the team's
    record. Never raises."""
    try:
        return _observe(team_dir, agent, events, judge, record, now)
    except Exception as e:
        return [f"triggers not learned from this turn ({type(e).__name__}: {e})"]


def _observe(team_dir, agent, events, judge, record, now) -> list[str]:
    cfg = triggers.config(team_dir)
    if cfg is None or not cfg.get("learn") or not events:
        return []
    events = plain([e for e in events if not _held(e)])
    mems = _memories(team_dir, agent)
    asks = int(cfg.get("judge_max") or 0) if judge is not None else 0
    credits, trouble, lines = [], [], []

    def say(event: str, **fields) -> None:
        if record:
            try:
                record(event, agent=agent, **fields)
            except Exception:
                pass

    # What was shown: did it bear on the call?
    for i, ev in enumerate(events):
        if ev.get("event") != "triggered":
            continue
        after = [e for e in events[i + 1:] if e.get("event") == "tool_call"][:FOLLOWING]
        if not after:
            continue
        for key, via in zip(ev.get("shown") or [], ev.get("via") or []):
            name = str(key).partition(":")[2]
            m = mems.get(name)
            if not str(key).startswith("memory:") or not m or not via:
                continue
            got = None
            if asks > 0:
                asks -= 1
                try:
                    got = _bore(judge, m, ev, after)
                    judged = True
                except Exception:
                    judged = False
            else:
                judged = False
            if not judged:
                nxt = next((e for e in after if triggers.shape(
                    str(e.get("tool") or ""), _args(e), sub=False) == ev.get("family")), None)
                if nxt is not None and not nxt.get("error") \
                        and _text(str(nxt.get("tool") or ""), _args(nxt))[:400] != ev.get("call"):
                    got = (0.5, 0.0, "the call came back changed and went through")
            if got:
                credits.append((name, via, via in m["written"], *got))
                say("trigger_verdict", memory=name, trigger=via, helped=got[0],
                    noise=got[1], why=got[2], judged=judged)

    # What went wrong and was made again: was there a memory for it?
    for ep in episodes(events):
        shown = [(i, ev) for i, ev in enumerate(events)
                 if ev.get("event") == "triggered" and ev.get("family") == ep["family"]]
        before = [str(k).partition(":")[2] for i, ev in shown if i < ep["at"]
                  for k in ev.get("shown") or [] if str(k).startswith("memory:")]
        entry = {k: ep[k] for k in ("ts", "shape", "family", "tool", "args", "call", "error",
                                    "tries", "worked", "looked_up")}
        entry.update(memory=None, how="")
        due = triggers.due_for(team_dir, agent, ep["tool"], ep["args"])
        if before:
            entry.update(memory=before[0], how="shown")
        elif any(ep["at"] <= i <= ep["until"] for i, _ in shown):
            entry.update(memory=next((d["title"] for d in due), None), how="late")
        elif due:
            # It has a memory, shown earlier in this conversation and not
            # yet due again: out of sight when it was needed.
            triggers.rearm_some(team_dir, agent, [d["key"] for d in due])
            entry.update(memory=due[0]["title"], how="faded")
            lines.append(f"'{due[0]['title']}' is shown again at the next {ep['shape']}: "
                         f"one went wrong with it out of sight")
        else:
            opened = []
            for e in events[ep["at"]:ep["until"] + 1]:
                n = str(_args(e).get("name") or "")
                if e.get("event") == "tool_call" and not e.get("error") and n in mems \
                        and triggers.tool_name(str(e.get("tool") or "")) == "read_memory" \
                        and n not in opened:
                    opened.append(n)
            pick = None
            if opened:
                pick = (opened[0], ep["shape"], "opened after the call went wrong")
            elif asks > 0 and mems:
                asks -= 1
                try:
                    hit = _helper(judge, mems, ep)
                    pick = hit and (*hit, "would have made a failed call go right")
                except Exception:
                    pick = None
            if pick and pick[1]:
                name, trig, why = pick
                credits.append((name, trig, trig in mems[name]["written"], 1.0, 0.0, why))
                entry.update(memory=name, how="opened" if opened else "judged")
        trouble.append(entry)
        say("trigger_trouble", shape=ep["shape"], tries=ep["tries"], call=ep["call"][:200],
            memory=entry["memory"], how=entry["how"])
        if not entry["memory"]:
            lines.append(f"{ep['shape']} took {ep['tries']} tries, with no memory on it")

    if not credits and not trouble:
        return lines
    path = _path(team_dir, agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with filelock.locked(path):
        state = _state(path)
        for name, trig, written, helped, noise, why in credits:
            change = _credit(state, cfg, name, trig, written, helped, noise, why, now)
            if change == "learned":
                lines.append(f"'{name}' now comes up before {trig} ({why})")
                say("trigger_learned", memory=name, trigger=trig, why=why)
            elif change == "back":
                lines.append(f"'{name}' comes up before {trig} again ({why})")
                say("trigger_learned", memory=name, trigger=trig, why=why, written=True)
            elif change == "muted":
                lines.append(f"'{name}' no longer comes up before {trig}: "
                             f"it has not borne on those calls")
                say("trigger_muted", memory=name, trigger=trig, written=written)
        state["trouble"] = (state["trouble"] + trouble)[-KEEP:]
        triggers._save(path, state)
    triggers.forget_cache()
    return lines


def observe_turn(team_dir, agent: str, since: str, record=None) -> list[str]:
    """observe() on the agent's record from `since`, with the team's judge."""
    events = turn_events(team_dir, agent, since)
    if not events:
        return []
    return observe(team_dir, agent, events, judge_for(team_dir), record)


# --- what the agent is told ---------------------------------------------------------

def _covered(team_dir, agent: str, t: dict) -> bool:
    """Whether a memory now comes up before a call like this one."""
    try:
        return bool(triggers.due_for(team_dir, agent, t.get("tool") or "", t.get("args") or {}))
    except Exception:
        return False


def _trouble_lines(rows: list[dict], limit: int = 5) -> list[str]:
    by: dict[str, dict] = {}
    for t in rows:
        g = by.setdefault(t.get("shape") or t.get("family") or "?", {**t, "tries": 0, "times": 0})
        g["tries"] += int(t.get("tries") or 0)
        g["times"] += 1
        if t.get("worked"):
            g["worked"] = t["worked"]
    out = []
    for shape, g in sorted(by.items(), key=lambda kv: -kv[1]["tries"])[:limit]:
        line = (f"- {shape}: {g['tries']} tries" + (f" over {g['times']} times" if g["times"] > 1 else "")
                + f". First: `{g['call'][:200]}` -> {' '.join(str(g['error']).split())[:240]}")
        if g.get("worked"):
            line += f". What worked: `{g['worked'][:200]}`"
        out.append(line)
    return out


MISSING = """
Calls that went wrong since you last wrote down what you learned, and had to be made again:
{rows}
Some of these are ordinary work. Where one was a mistake about how to make the call, save what would have made the first try go right: what to run, in what order, the flag or the step you missed, enough that the next one goes right without looking anything up. Give it `triggers` naming the call (e.g. triggers="{example}") and it is put in front of you before the next one.{thin}"""


def missing(team_dir, agent: str, mark: bool = True) -> str:
    """For the agent writing down what it learned: the calls that went
    wrong since it last did, that no memory of its comes up before yet.
    "" when there are none. Marks them told."""
    try:
        path = _path(team_dir, agent)
        if not path.exists() or not (triggers.config(team_dir) or {}).get("learn"):
            return ""
        with filelock.locked(path):
            state = _state(path)
            told = state.get("told") or ""
            new = [t for t in state["trouble"] if (t.get("ts") or "") > told]
            if mark and new:
                state["told"] = max(t.get("ts") or "" for t in new)
                triggers._save(path, state)
        open_ = [t for t in new if not t.get("memory") and not _covered(team_dir, agent, t)]
        thin = sorted({t["memory"] for t in new if t.get("how") == "shown" and t.get("memory")})
        if not open_ and not thin:
            return ""
        rows = _trouble_lines(open_) or ["- (none without a memory)"]
        example = next((t["shape"] for t in open_ if t.get("shape")), "Bash(kubectl apply)")
        note = (f"\nYou were shown {', '.join(repr(n) for n in thin)} before a call that then "
                f"went wrong anyway: add what was missing to it." if thin else "")
        return MISSING.format(rows="\n".join(rows), example=example, thin=note)
    except Exception:
        return ""


def review(team_dir, agent: str, since: str) -> str:
    """For the agent reviewing its memories: what its triggers did since
    `since`. "" when nothing did."""
    try:
        path = _path(team_dir, agent)
        if not path.exists() or not (triggers.config(team_dir) or {}).get("learn"):
            return ""
        state = _state(path)
        learned, muted = standing(team_dir, agent)
        out = []
        for n in state["news"]:
            if (n.get("ts") or "") <= since:
                continue
            m, t = n.get("memory"), n.get("trigger")
            if n.get("kind") == "learned" and t in learned.get(m, []):
                out.append(f"- learned: '{m}' now comes up before {t} ({n.get('why')}). Save it "
                           f"with that trigger to keep it for good, or with triggers=\"none\" "
                           f"if it should not.")
            elif n.get("kind") == "muted" and (m, t) in muted:
                out.append(f"- no longer shown: '{m}' before {t}. It was shown and did not bear "
                           f"on the call ({n.get('why')}). Narrow the trigger to the calls the "
                           f"memory is about, or remove it.")
        rows = [t for t in state["trouble"] if (t.get("ts") or "") > since]
        thin = sorted({t["memory"] for t in rows if t.get("how") == "shown" and t.get("memory")})
        for name in thin:
            out.append(f"- shown, and the call still went wrong: '{name}'. Add what was missing.")
        open_ = [t for t in rows if not t.get("memory") and not _covered(team_dir, agent, t)]
        if open_:
            out.append("- calls made wrong and again, with no memory on them (save the lesson "
                       "with triggers if there is one):")
            out += ["  " + line for line in _trouble_lines(open_)]
        if not out:
            return ""
        return (f"\nWhat came up before your calls since your last review ({since[:16]}), "
                f"judged by what happened after:\n" + "\n".join(out) + "\n")
    except Exception:
        return ""


def report(team_dir, agent: str) -> str:
    """The record as a table: every trigger with evidence, and whether it
    is shown."""
    cfg = triggers.config(team_dir) or dict(triggers.DEFAULTS)
    state = _state(_path(team_dir, agent))
    rows = sorted(state["scores"].values(), key=lambda r: (r["memory"], r["trigger"]))
    out = [f"{agent}: {len(rows)} trigger(s) with evidence; shown at {cfg['show_above']:.2f} "
           f"and above"]
    for r in rows:
        s = score(r)
        out.append(f"  {s:4.2f} {'shown ' if s >= cfg['show_above'] else 'silent'} "
                   f"{'written' if r.get('written') else 'learned'}  {r['memory']}  "
                   f"{r['trigger']}  (helped {r['helped']:.1f}, not {r['noise']:.1f}, "
                   f"{r['n']} verdicts; last: {r.get('why', '')})")
    open_ = [t for t in state["trouble"] if not t.get("memory")]
    if open_:
        out.append("  calls made wrong and again, with no memory on them:")
        out += ["  " + line for line in _trouble_lines(open_, 10)]
    return "\n".join(out)


if __name__ == "__main__":
    import os
    import sys

    from . import roster, scope
    td = scope.team_dir(os.environ.get("ATEAM_TEAM_DIR"))
    names = sys.argv[1:] or [a["name"] for a in roster.load(Path(td) / "roster.json").get("agents", [])]
    print("\n".join(report(td, n) for n in names))

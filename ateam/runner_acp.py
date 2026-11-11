"""Runner for any agent harness that speaks ACP, the Agent Client Protocol.

    ATEAM_RUNNER=ateam.runner_acp:AcpRunner
    ATEAM_RUNNER_CONFIG='{"agent": "opencode"}'

or per agent, beside agents on other runtimes (runner_mixed):

    {"name": "coder", "runner": "ateam.runner_acp:AcpRunner",
     "model": "google/gemini-3.8-flash"}

a-team is not an agent harness. It keeps a team of durable agents, each of
which runs in one: Claude Code, Codex, agy -- and, through this runner, any
harness that speaks ACP (OpenCode, Grok Build, Goose, Gemini CLI, and the
adapters for others). ACP is the protocol editors use to drive agents: a
session, a prompt, a stream of what the agent does, a permission request
before a tool runs. That is enough for a teammate: the session survives
between turns, and `session/new` takes the MCP servers through which the
agent reaches the team's bus, memory and tasks.

Each turn starts the harness (`opencode acp`), resumes the agent's session,
sets its model, sends the wake, records what the agent does as it happens,
and exits, so a supervisor restart loses nothing. A new session's first
prompt carries the agent's brief: ACP has no system prompt of its own.

What ACP does not give, and what this runner therefore does not claim:

- Policy before every tool call. A client is asked only about the calls the
  harness asks permission for -- and then, for some tools, without their
  arguments. Those questions are answered from the team's policy. For
  OpenCode a plugin of a-team's (acp_plugins/opencode) checks every call with
  its full arguments, and keeps the keys that run agents out of the commands
  they run; for other harnesses, set their own permission mode to ask.
- Withholding a tool. `tools_off` refuses the call; the model still sees it.
- Containment. The harness runs commands as the supervisor's user.
- Quota. Token counts and cost are recorded when the harness reports them
  (OpenCode does); account limits are not reported over ACP.

Config (ATEAM_RUNNER_CONFIG or the roster's runner_config), all optional:

    {"agent": "opencode",              # a preset below, or:
     "command": ["/path/to/agent", "acp"],
     "env": {"NAME": "value"},         # added to the harness's environment
     "model": "provider/model",        # when the roster names none
     "models": ["provider/model"],     # offered without asking the harness
     "timeout": 600}

The harness's own configuration -- providers, sign-ins, AGENTS.md -- is
used as it is. Keys come from the supervisor's environment, never the
roster.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config as cfg
from . import hook_pre_tool_use
from . import persona
from . import roster as roster_lib
from . import scope
from . import steer
from . import triggers
from .runner import Runner, capacity_note, with_retry

SERVERS = ("memory", "bus", "tasks", "self")
PROTOCOL_VERSION = 1

#: Harnesses by name. Only OpenCode has been run against this runner; the
#: others are their documented ACP commands.
PRESETS = {
    "opencode": {"command": ["opencode", "acp"], "plugin_dir": "opencode",
                 "env": {"OPENCODE_DISABLE_AUTOUPDATE": "1"}},
    "grok": {"command": ["grok", "agent", "stdio", "--no-auto-update"]},
    "goose": {"command": ["goose", "acp"]},
    "gemini": {"command": ["gemini", "--experimental-acp"]},
}

#: Keys that run agents, kept out of the commands agents run where the
#: harness lets us (OpenCode, through the plugin).
KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY",
        "OPENCODE_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "DEEPSEEK_API_KEY")

PLUGINS = Path(__file__).resolve().parent / "acp_plugins"


class AcpError(RuntimeError):
    """An error answer from the harness: its message, worded so with_retry
    and the fallback can read it (a 429 says so)."""


def team_tool(name: str) -> str:
    """ateam_bus_send_to_teammate, as OpenCode names an MCP tool, is the
    policy's send_to_teammate."""
    for s in SERVERS:
        for prefix in (f"ateam_{s}_", f"ateam_{s}."):
            if name.startswith(prefix):
                return name[len(prefix):]
    return name


class _Conn:
    """One harness process, spoken to over stdio as JSON-RPC lines."""

    def __init__(self, argv: list[str], env: dict, cwd: str):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
                                     encoding="utf-8", errors="replace")
        self.inbox: queue.Queue = queue.Queue()
        self.stderr: list[str] = []
        self._next = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                pass
        self.inbox.put(None)

    def _drain(self):
        for line in self.proc.stderr:
            self.stderr = (self.stderr + [line.rstrip()])[-20:]

    def send(self, method: str, params: dict | None = None, notify: bool = False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._next += 1
            msg["id"] = self._next
        self.write(msg)
        return msg.get("id")

    def write(self, msg: dict) -> None:
        with self._lock:
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass                    # it exited; the reader says so

    def next(self, timeout: float):
        """The next message, None once the process has exited, or raises Empty."""
        return self.inbox.get(timeout=timeout)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class AcpRunner(Runner):
    label = "acp"
    # Audit: every tool call the harness reports, as it starts. Scoping and
    # containment: see the module docstring.
    supports_audit = True
    supports_containment = False
    supports_capability_scoping = False
    # The model is a session setting, changed between turns.
    switches_model_in_conversation = True

    def __init__(self, config=None, observer=None):
        super().__init__(config, observer=observer)
        preset = PRESETS.get(str(self.config.get("agent") or "opencode"), {})
        self.preset_name = str(self.config.get("agent") or "opencode")
        self.command = list(self.config.get("command") or preset.get("command") or [])
        self.plugin_dir = preset.get("plugin_dir") if not self.config.get("command") else None
        self.extra_env = {**(preset.get("env") or {}), **(self.config.get("env") or {})}
        self.model = self.config.get("model")
        self.timeout = self.config.get("timeout", cfg.TURN_TIMEOUT_S)
        self.label = self.preset_name if not self.config.get("command") else "acp"
        self.scopes = scope.load()
        self._live: dict[str, threading.Event] = {}

    # --- what it offers ---------------------------------------------------------

    @classmethod
    def list_models(cls, config=None):
        """The models the harness offers a new session, asked of it once
        (the caller caches), plus any the config names."""
        config = config or {}
        extra = [{"id": m, "label": m} for m in config.get("models") or []]
        if config.get("models") and config.get("ask_models") is False:
            return extra
        try:
            r = cls(config)
            found = r._ask_models()
        except Exception:
            found = None
        if found is None:
            return extra or None
        seen = {m["id"] for m in found}
        return found + [m for m in extra if m["id"] not in seen]

    def _ask_models(self) -> list[dict] | None:
        if not self._binary():
            return None
        with tempfile.TemporaryDirectory() as d:
            conn = _Conn(self._argv(), self._env("", d), d)
            try:
                deadline = time.monotonic() + 60
                self._call(conn, "initialize", self._init_params(), deadline)
                res = self._call(conn, "session/new", {"cwd": d, "mcpServers": []}, deadline)
                opt = _model_option(res)
                if res.get("sessionId"):
                    conn.send("session/close", {"sessionId": res["sessionId"]})
                if not opt:
                    return None
                return [{"id": o.get("value"), "label": o.get("name") or o.get("value")}
                        for o in opt.get("options") or [] if o.get("value")]
            finally:
                conn.close()

    # --- the harness ----------------------------------------------------------------

    def _binary(self) -> str | None:
        if not self.command:
            return None
        first = self.command[0]
        return first if os.path.isabs(first) and os.path.exists(first) else shutil.which(first)

    def _argv(self) -> list[str]:
        return [self._binary() or self.command[0], *self.command[1:]]

    def _env(self, agent: str, cwd: str) -> dict:
        env = dict(os.environ)
        env.update({k: str(v) for k, v in self.extra_env.items()})
        if agent:
            env.update({"ATEAM_AGENT": agent, "ATEAM_TEAM_DIR": str(self.team_dir)})
        if self.plugin_dir:
            env["OPENCODE_CONFIG_DIR"] = str(PLUGINS / self.plugin_dir)
            env["ATEAM_PYTHON_BIN"] = sys.executable
            env["PYTHONPATH"] = os.pathsep.join(
                p for p in (str(cfg.PROJECT_ROOT), env.get("PYTHONPATH", "")) if p)
            env["ATEAM_STRIP_KEYS"] = ",".join(KEYS)
        return env

    @staticmethod
    def _init_params() -> dict:
        return {"protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                       "terminal": False},
                "clientInfo": {"name": "a-team", "version": _version()}}

    def _spec(self, agent: str) -> tuple[dict, list[dict], list[str]]:
        roster = roster_lib.load(self.team_dir / "roster.json")
        spec = next((a for a in roster["agents"] if a["name"] == agent),
                    {"name": agent, "role": ""})
        spaces = list(dict.fromkeys([*roster.get("workspaces", []),
                                     *spec.get("workspaces", [])]))
        return spec, roster["agents"], spaces

    def _mcp_servers(self, agent: str) -> list[dict]:
        env = {"PYTHONPATH": str(cfg.PROJECT_ROOT), "ATEAM_AGENT": agent,
               "ATEAM_TEAM_DIR": str(self.team_dir)}
        for k in ("ATEAM_TEAM", "ATEAM_TEAMS_ROOT", "ATEAM_DURABLE_DIR", "ATEAM_AUDIT_LOG",
                  "ATEAM_BUS_TRANSPORT", "ATEAM_BUS_CONFIG", "ATEAM_MEMORY_STORE",
                  "ATEAM_MEMORY_CONFIG"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        workspace = str(self.scopes.agent_workspace(agent))
        return [{"name": f"ateam_{s}", "command": sys.executable,
                 "args": ["-m", f"ateam.mcp_{s}", *([workspace] if s == "memory" else [])],
                 "env": [{"name": k, "value": v} for k, v in env.items()]}
                for s in SERVERS]

    def _call(self, conn: _Conn, method: str, params: dict, deadline: float, on_message=None):
        """Send a request and wait for its answer, handling what comes first."""
        rid = conn.send(method, params)
        while True:
            m = conn.next(max(0.1, deadline - time.monotonic()))
            if m is None:
                tail = " | ".join(conn.stderr[-3:])
                raise RuntimeError(f"the agent process exited{': ' + tail if tail else ''}")
            if m.get("id") == rid and "method" not in m:
                if "error" in m:
                    err = m["error"] if isinstance(m["error"], dict) else {"message": m["error"]}
                    data = err.get("data")
                    raise AcpError(f"{err.get('message') or 'error'}"
                                   + (f": {json.dumps(data, default=str)[:400]}" if data else ""))
                return m.get("result") or {}
            if on_message:
                on_message(m)
            elif "id" in m and m.get("method"):
                conn.write({"jsonrpc": "2.0", "id": m["id"],
                            "error": {"code": -32601, "message": "not now"}})

    # --- one turn ---------------------------------------------------------------------

    def _run(self, agent: str, message: str, cancel: threading.Event) -> str:
        t0 = time.monotonic()
        deadline = t0 + (self.timeout or 86400)
        spec, agents, spaces = self._spec(agent)
        model, _ = self.resolved_model(agent, spec.get("model") or self.model, None)
        cwd = spaces[0] if spaces else str(Path.cwd())
        texts, calls, usage = [], {}, {"turn": None, "used": None, "cost": None}
        state = {"session": None, "replaying": False}
        conn = _Conn(self._argv(), self._env(agent, cwd), cwd)

        def on_message(m: dict) -> None:
            method, p = m.get("method", ""), m.get("params") or {}
            if "id" in m and method:
                self._answer(conn, agent, state, m, cwd)
            elif method == "session/update" and not state["replaying"]:
                self._update(agent, state, p.get("update") or {}, texts, calls, usage)

        try:
            init = self._call(conn, "initialize", self._init_params(), deadline, on_message)
            caps = (init.get("agentCapabilities") or {})
            session_caps = caps.get("sessionCapabilities") or {}
            servers = self._mcp_servers(agent)
            conv, fresh, res = self.conversation_id(agent), False, {}
            if conv:
                try:
                    if "resume" in session_caps:
                        res = self._call(conn, "session/resume", {"sessionId": conv, "cwd": cwd,
                                                                  "mcpServers": servers},
                                         deadline, on_message)
                    elif caps.get("loadSession"):
                        state["replaying"] = True       # the history is not this turn's
                        res = self._call(conn, "session/load", {"sessionId": conv, "cwd": cwd,
                                                                "mcpServers": servers},
                                         deadline, on_message)
                        state["replaying"] = False
                    else:
                        conv = None
                except AcpError as e:
                    state["replaying"] = False
                    print(f"    {agent}: session {conv} not resumed ({e}), starting a new one",
                          flush=True)
                    self.reset(agent)
                    conv = None
            if not conv:
                res = self._call(conn, "session/new", {"cwd": cwd, "mcpServers": servers},
                                 deadline, on_message)
                conv, fresh = res["sessionId"], True
                # Bound before the work is delivered (Runner.wake).
                self.remember_conversation(agent, conv)
            state["session"] = conv
            err = self._set_model(conn, conv, res, model, deadline, on_message)
            if err:
                return err
            text = message
            if fresh:
                text = (persona.brief(agent, agents, self.scopes.shared_dir())
                        + "\n\n---\n\n" + message)
            rid = conn.send("session/prompt", {"sessionId": conv,
                                               "prompt": [{"type": "text", "text": text}]})
            result, cancelled = None, False
            while result is None:
                if (cancel.is_set() or time.monotonic() > deadline) and not cancelled:
                    cancelled = True
                    conn.send("session/cancel", {"sessionId": conv}, notify=True)
                    deadline = time.monotonic() + 30
                try:
                    m = conn.next(0.5)
                except queue.Empty:
                    if cancelled and time.monotonic() > deadline:
                        break
                    continue
                if m is None:
                    tail = " | ".join(conn.stderr[-3:])
                    return f"[error: {self.label} exited mid-turn{': ' + tail if tail else ''}]"
                if m.get("id") == rid and "method" not in m:
                    if "error" in m:
                        e = m["error"] if isinstance(m["error"], dict) else {"message": m["error"]}
                        return f"[error: {self.label}: {e.get('message') or e}]"
                    result = m.get("result") or {}
                else:
                    on_message(m)
        except AcpError as e:
            return f"[error: {self.label}: {e}]"
        except RuntimeError as e:
            return f"[error: {self.label}: {e}]"
        except queue.Empty:
            return f"[error: {self.label} did not answer within {self.timeout:.0f}s]"
        finally:
            conn.close()

        stop = (result or {}).get("stopReason")
        usage["turn"] = (result or {}).get("usage")
        self._record(agent, conv, model, t0, usage)
        if cancel.is_set():
            return "[error: turn cancelled]"
        if result is None or stop == "cancelled":
            # Not the word with_retry waits out: this turn had its time,
            # unless it spent it being refused for capacity.
            return f"[error: the turn ran past {self.timeout:.0f}s{capacity_note(conn.stderr)}]"
        if stop == "refusal":
            return f"[error: {self.label}: the model refused to continue]"
        return "".join(texts).strip()

    def _set_model(self, conn, conv, res, model, deadline, on_message) -> str | None:
        """Put the session on `model` if it is not on it. An error to return,
        or None."""
        if not model:
            return None
        opt = _model_option(res)
        if opt is None:
            return None                 # the harness does not say; its own default runs
        if opt.get("currentValue") == model:
            return None
        values = [o.get("value") for o in opt.get("options") or []]
        if values and model not in values:
            return (f"[error: {self.label} does not offer the model {model!r} "
                    f"(model not found); `python -m ateam.models available` lists what it does]")
        try:
            self._call(conn, "session/set_config_option",
                       {"sessionId": conv, "configId": opt.get("id") or "model", "value": model},
                       deadline, on_message)
        except AcpError as e:
            return f"[error: {self.label} would not switch to {model!r}: {e}]"
        return None

    def _answer(self, conn: _Conn, agent: str, state: dict, m: dict, cwd: str) -> None:
        """A request from the harness. Nobody is there to ask: a permission
        request is answered from the team's policy, anything else declined."""
        method, p = m.get("method"), m.get("params") or {}
        if method == "session/request_permission":
            call = p.get("toolCall") or {}
            prior = state.get("calls", {}).get(call.get("toolCallId"), {})
            tool = team_tool(str(call.get("name") or prior.get("tool") or call.get("title") or ""))
            raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
            args = {**prior.get("args", {}), **raw}
            args.setdefault("cwd", cwd)
            refusal = check(agent, tool, args, self.team_dir)
            options = p.get("options") or []
            want = ("reject_once", "reject_always") if refusal else ("allow_once", "allow_always")
            pick = next((o for k in want for o in options if o.get("kind") == k), None)
            outcome = ({"outcome": "selected", "optionId": pick["optionId"]} if pick
                       else {"outcome": "cancelled"})
            if refusal:
                print(f"    {agent}: refused {tool}: {refusal[:200]}", flush=True)
            conn.write({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": outcome}})
            return
        conn.write({"jsonrpc": "2.0", "id": m["id"],
                    "error": {"code": -32601, "message": f"{method} is not available here"}})

    def _update(self, agent, state, u: dict, texts, calls, usage) -> None:
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = u.get("content") or {}
            if c.get("type") == "text":
                texts.append(c.get("text") or "")
        elif kind == "tool_call":
            tid = u.get("toolCallId")
            raw = u.get("rawInput") if isinstance(u.get("rawInput"), dict) else {}
            name = team_tool(str(u.get("name") or u.get("title") or "tool"))
            calls[tid] = {"tool": name, "args": raw, "t0": time.monotonic(), "audited": False}
            state.setdefault("calls", {})[tid] = calls[tid]
        elif kind == "tool_call_update":
            c = calls.get(u.get("toolCallId"))
            if c is None:
                return
            if isinstance(u.get("rawInput"), dict) and u["rawInput"]:
                c["args"] = {**c["args"], **u["rawInput"]}
            # Audited once it runs: a pending call's arguments may still be
            # on their way (OpenCode sends a command with the next update).
            if not c["audited"] and u.get("status") == "in_progress":
                self._audit(agent, c)
            if u.get("status") in ("completed", "failed"):
                self._finished(agent, state, c, u)
        elif kind == "usage_update":
            usage["used"] = u.get("used")
            cost = u.get("cost") or {}
            if cost.get("amount") is not None and cost.get("currency", "USD") == "USD":
                usage["cost"] = cost["amount"]

    def _audit(self, agent: str, c: dict) -> None:
        c["audited"] = True
        hook_pre_tool_use.audit(agent, c["tool"], c["args"])

    def _finished(self, agent, state, c: dict, u: dict) -> None:
        if not c["audited"]:
            self._audit(agent, c)
        out = "".join(((x.get("content") or {}).get("text") or "")
                      for x in u.get("content") or [] if isinstance(x, dict))
        if not out and u.get("rawOutput") is not None:
            out = json.dumps(u["rawOutput"], default=str)
        failed = u.get("status") == "failed"
        try:
            self.observer.record_tool_call(
                agent=agent, conversation=state["session"] or "", tool=c["tool"], args=c["args"],
                result=None if failed else out[:4000], error=out[:4000] if failed else None,
                duration_s=max(0.0, time.monotonic() - c["t0"]))
        except Exception:
            pass                        # accounting must never break a turn
        steer.count_call(self.team_dir, agent, c["tool"], c["args"])

    def _record(self, agent: str, conv: str, model: str | None, t0: float, usage: dict) -> None:
        u = usage["turn"] or {}
        cost = None
        if usage["cost"] is not None:
            # The harness reports the session's running total.
            cost = _cost_delta(self.team_dir, conv, usage["cost"])
        cached = u.get("cachedReadTokens")
        try:
            self.observer.record_turn(
                agent=agent, conversation=conv, duration_s=time.monotonic() - t0,
                input_tokens=u.get("inputTokens"), output_tokens=u.get("outputTokens"),
                cache_read_tokens=cached, total_tokens=u.get("totalTokens"),
                model=model, context_tokens=usage["used"], cost_usd=cost, **self.meta_for(agent))
        except Exception:
            pass                        # accounting must never break a turn

    def wake(self, agent: str, message: str) -> str:
        t0 = time.monotonic()
        if not self._binary():
            out = (f"[error: {agent} cannot start: {self.command[0] if self.command else 'the agent'} "
                   f"is not installed, or set \"command\" in the runner config]")
        else:
            cancel = self._live.setdefault(agent, threading.Event())
            cancel.clear()

            def announce(attempt, delay, err):
                print(f"    {agent}: provider unavailable, retrying in {delay:.0f}s "
                      f"(attempt {attempt})", flush=True)
            try:
                out = with_retry(lambda: self._run(agent, message, cancel), on_wait=announce,
                                 on_capacity=lambda err: self.cool_down(agent, err))
            except Exception as e:
                out = f"[error: {agent} failed: {type(e).__name__}: {e}]"
            finally:
                self._live.pop(agent, None)
        if out.startswith("[error:"):
            try:
                self.observer.record_failure(agent, self.conversation_id(agent) or "", out,
                                             duration_s=time.monotonic() - t0)
            except Exception:
                pass
        return out

    def cancel(self, handle: dict) -> None:
        """Cancel the turn where it runs (session/cancel)."""
        super().cancel(handle)
        live = self._live.get(handle.get("agent") or "")
        if live:
            live.set()


def _model_option(res: dict) -> dict | None:
    """The session's model setting, from a session/new, /resume or /load
    answer: a config option of category "model", or the older "models"."""
    for o in res.get("configOptions") or []:
        if isinstance(o, dict) and (o.get("category") == "model" or o.get("id") == "model"):
            return o
    m = res.get("models")
    if isinstance(m, dict) and m.get("availableModels"):
        return {"id": "model", "currentValue": m.get("currentModelId"),
                "options": [{"value": x.get("modelId"), "name": x.get("name")}
                            for x in m["availableModels"]]}
    return None


def _cost_delta(team_dir, conv: str, total: float) -> float | None:
    """This turn's cost, from the session's running total and the last one
    seen (kept in <team_dir>/acp_costs.json, so a restart does not lose it)."""
    path = Path(team_dir) / "acp_costs.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc = doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        doc = {}
    before = doc.get(conv)
    doc[conv] = total
    try:
        path.write_text(json.dumps(doc), encoding="utf-8")
    except OSError:
        pass
    if before is None or total < before:
        return None if before is not None else total
    return round(total - before, 6)


def check(agent: str, tool: str, args: dict, team_dir=None) -> str | None:
    """The team's policy on one call, in a-team's names for it."""
    from . import policy
    tool = team_tool(tool)
    refusal = policy.check_tool_policy(agent, tool, args or {},
                                       team_dir=team_dir or os.environ.get("ATEAM_TEAM_DIR"))
    if refusal:
        hook_pre_tool_use.audit(agent, tool, args or {}, refusal)
    return refusal


def _version() -> str:
    from . import __version__
    return __version__


def main(argv=None) -> int:
    """`python -m ateam.runner_acp check`: the policy, for a harness plugin.
    Reads {"agent", "tool", "args", "cwd"} on stdin; exits 2 with the refusal
    on stdout, or 0."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["check"]:
        print(__doc__.split("\n\n")[0])
        return 1
    try:
        doc = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    args = dict(doc.get("args") or {})
    if doc.get("cwd"):
        args.setdefault("cwd", doc["cwd"])
    agent = str(doc.get("agent") or os.environ.get("ATEAM_AGENT") or "")
    refusal = check(agent, str(doc.get("tool") or ""), args)
    # Held here, where the reason reaches the model, and not on a permission
    # request (_answer), whose refusal carries none.
    refusal = refusal or triggers.hold(os.environ.get("ATEAM_TEAM_DIR"), agent,
                                       team_tool(str(doc.get("tool") or "")), args)
    if refusal:
        print(refusal)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Runner that wakes agents on OpenAI models, through Codex's app-server.

    ATEAM_RUNNER=ateam.runner_codex:CodexRunner
    OPENAI_API_KEY=...          # in the environment, never in the roster

or per agent, beside agents on other runtimes (runner_mixed):

    {"name": "qa", "runner": "ateam.runner_codex:CodexRunner",
     "model": "gpt-6-astra", "effort": "high"}

Codex is to OpenAI what Claude Code is to Claude: the agent loop, a shell and
a patch tool, an MCP client, threads that resume from disk and compact
themselves. The `openai` library alone would leave a-team to write and keep up
all of that. Each turn starts `codex app-server`, resumes the agent's thread,
runs one turn and exits, so a supervisor restart loses nothing. Teammates are
reached through the same MCP servers as on every other runtime.

The key comes from the environment and only from there: the model provider is
defined here with `env_key`, and threads live under <team_dir>/codex, a Codex
home with no login in it. A machine's own Codex sign-in, a ChatGPT plan, is
never used, and nothing is written to auth.json. Any endpoint that speaks the
Responses API can stand in for OpenAI's ("provider" below).

Commands run in Codex's sandbox: writes are held to the agent's workspaces by
the kernel, network on unless the config turns it off. Reads are not held,
so this does not claim containment: an agent can read anything the supervisor
can. Nothing is asked of a person mid-turn: a command the sandbox refuses
fails, and the agent sees that.

Requires the Codex CLI, 0.166 or later (`npm install -g @openai/codex`), or
"codex" in the config naming one.

Config (ATEAM_RUNNER_CONFIG), all optional:
    {"model": "gpt-6.1-sol", "effort": "medium", "workers": true,
     "timeout": 600, "max_turn_tokens": 1000000, "network": true,
     "web_search": true, "codex": "/path/to/codex",
     "provider": {"base_url": "https://api.openai.com/v1",
                  "env_key": "OPENAI_API_KEY"},
     "models": ["an-id-newer-than-this-file"]}
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as cfg
from . import hook_pre_tool_use
from . import persona
from . import roster as roster_lib
from . import scope
from . import steer
from .runner import Runner, capacity_note, with_retry
from .transport import load as load_transport

DEFAULT_MODEL = "gpt-6.1-sol"
#: The manager and whoever reviews, as on the Claude runner.
STRONG_MODEL = "gpt-6-astra"
#: Tokens a turn may spend, not counting cached input. Codex has no dollar
#: cap, and a turn has no other ceiling. "max_turn_tokens": null turns it off.
DEFAULT_TURN_TOKENS = 1_000_000
MODELS = [{"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"},
          {"id": "gpt-6-astra", "label": "GPT-6 Astra"},
          {"id": "gpt-6-luna", "label": "GPT-6 Luna"},
          {"id": "gpt-6.1-terra", "label": "GPT-6.1 Terra"}]
PROVIDER = {"name": "a-team", "base_url": "https://api.openai.com/v1",
            "env_key": "OPENAI_API_KEY", "wire_api": "responses"}

# Effort as a roster writes it, for any runtime, to what this one takes.
EFFORT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high",
          "extra_high": "xhigh", "xhigh": "xhigh", "max": "max"}

# On by default in Codex, and not for an unattended teammate: a browser, the
# desktop, plugins and apps a person installs, a daemon that outlives the turn.
# A fixed surface, as on the Claude runner.
FEATURES_OFF = ("apps", "browser_use", "browser_use_external", "computer_use",
                "image_generation", "in_app_browser", "plugins", "remote_plugin",
                "plugin_discovery_prefetch", "skill_mcp_dependency_install",
                "tool_suggest", "goals", "daemon_auto_start", "fast_mode",
                "ultrafast_mode", "hooks")

# A roster's tools_off, in any runtime's names, to what withholds it here.
SHELL = ("shell_tool", "unified_exec")
FEATURE_FOR = {"run_command": SHELL, "Bash": SHELL,
               "invoke_subagent": ("multi_agent",), "Agent": ("multi_agent",)}
WEB = ("search_web", "WebSearch", "read_url_content", "WebFetch")

# Keys that run agents, kept from every command an agent runs.
KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY")

SERVERS = ("memory", "bus", "tasks", "self")

# Out of credit, by Codex's name for it: the supervisor stops on these words.
HALT = ("usageLimitExceeded", "sessionBudgetExceeded")
WAIT = ("rateLimitExceeded", "serverOverloaded")


def _strong(agent: str, spec: dict) -> bool:
    words = f"{agent} {spec.get('role') or ''}".lower()
    return bool(spec.get("is_principal") or spec.get("principal")) or any(
        w in words for w in ("manager", "faces outward", "review", "qa"))


def _toml(value) -> str:
    """A value for `codex -c key=value`, which reads TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(str(value))


def _error_text(err: dict | None) -> str:
    """A turn's error, worded so with_retry and the budget stop can read it."""
    err = err or {}
    info = err.get("codexErrorInfo")
    name = info if isinstance(info, str) else next(iter(info), "") if isinstance(info, dict) else ""
    msg = str(err.get("message") or "the turn failed")[:500]
    if name in HALT:
        msg += " (insufficient_quota)"
    elif name in WAIT:
        msg += " (rate limit)"
    return f"[error: {name or 'codex'}: {msg}]"


class _Server:
    """One `codex app-server`, spoken to over stdio as JSON-RPC lines."""

    def __init__(self, argv: list[str], env: dict, cwd: str):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
                                     encoding="utf-8", errors="replace")
        self.inbox: queue.Queue = queue.Queue()
        self.stderr: list[str] = []
        self._next = 0
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
        msg = {"method": method, "params": params or {}}
        if not notify:
            self._next += 1
            msg["id"] = self._next
        self.reply(msg)
        return msg.get("id")

    def reply(self, msg: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass                        # it exited; the reader says so

    def next(self, timeout: float):
        """The next message, None once the server has exited, or raises Empty."""
        return self.inbox.get(timeout=timeout)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class CodexRunner(Runner):
    label = "codex"
    # Audit: every command, edit, team tool and search is logged as it starts,
    # from the turn's own events. Codex runs a command without asking, so this
    # records rather than vetoes; the team's own tools still check the policy
    # where they run. Scoping: a withheld tool is a feature turned off, so the
    # model is never offered it. Containment: writes only (see above).
    supports_audit = True
    supports_containment = False
    supports_capability_scoping = True

    def __init__(self, config=None, observer=None):
        super().__init__(config, observer=observer)
        self.model = self.config.get("model")
        self.effort = self.config.get("effort", "medium")
        self.timeout = self.config.get("timeout", cfg.TURN_TIMEOUT_S)
        self.use_workers = self.config.get("workers", True)
        self.provider = {**PROVIDER, **(self.config.get("provider") or {})}
        self.scopes = scope.load()
        self._live: dict[str, threading.Event] = {}    # agent -> cancel its turn

    @classmethod
    def list_models(cls, config=None):
        extra = (config or {}).get("models") or []
        return MODELS + [{"id": m, "label": m} for m in extra]

    def _codex(self) -> str | None:
        return self.config.get("codex") or shutil.which("codex")

    def _spec(self, agent: str) -> tuple[dict, list[dict], list[str]]:
        roster = roster_lib.load(self.team_dir / "roster.json")
        spec = next((a for a in roster["agents"] if a["name"] == agent),
                    {"name": agent, "role": ""})
        spaces = list(dict.fromkeys([*roster.get("workspaces", []),
                                     *spec.get("workspaces", [])]))
        return spec, roster["agents"], spaces

    def _model_for(self, agent: str, spec: dict) -> str:
        return spec.get("model") or self.model or (STRONG_MODEL if _strong(agent, spec) else DEFAULT_MODEL)

    def _turn_tokens(self, spec: dict) -> int | None:
        return spec.get("max_turn_tokens", self.config.get("max_turn_tokens", DEFAULT_TURN_TOKENS))

    def _mcp_servers(self, agent: str) -> dict:
        # Notes go by turn/steer (_after): a team tool's result reaches the
        # model only if the model's script prints it.
        env = {"PYTHONPATH": str(cfg.PROJECT_ROOT), "ATEAM_AGENT": agent,
               "ATEAM_TEAM_DIR": str(self.team_dir), "ATEAM_NOTES_BY_RUNNER": "1"}
        for k in ("ATEAM_TEAM", "ATEAM_TEAMS_ROOT", "ATEAM_DURABLE_DIR", "ATEAM_AUDIT_LOG",
                  "ATEAM_BUS_TRANSPORT", "ATEAM_BUS_CONFIG", "ATEAM_MEMORY_STORE",
                  "ATEAM_MEMORY_CONFIG"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        workspace = str(self.scopes.agent_workspace(agent))
        # Approved without asking: the team's tools check the policy themselves,
        # where they run, and Codex would otherwise refuse every call to them.
        return {f"ateam_{s}": {"command": sys.executable, "env": env,
                               "args": ["-m", f"ateam.mcp_{s}", *([workspace] if s == "memory" else [])],
                               "default_tools_approval_mode": "approve"}
                for s in SERVERS}

    def _argv(self, agent: str, spec: dict, spaces: list[str]) -> list[str]:
        off = set(FEATURES_OFF)
        tools_off = spec.get("tools_off") or []
        team_off = []
        for name in tools_off:
            if name in FEATURE_FOR:
                off.update(FEATURE_FOR[name])
            elif name not in WEB:
                team_off.append(name)
        if not (self.use_workers and spec.get("workers", True)):
            off.add("multi_agent")
        web = self.config.get("web_search", True) and not any(n in WEB for n in tools_off)
        servers = self._mcp_servers(agent)
        if team_off:
            for s in servers.values():
                s["disabled_tools"] = team_off
        roots = [*spaces, str(self.scopes.agent_workspace(agent)), str(self.scopes.shared_dir())]
        overrides = {
            "model_provider": "ateam",
            "model_providers.ateam": self.provider,
            "mcp_servers": servers,
            "sandbox_workspace_write": {"writable_roots": roots,
                                        "network_access": bool(self.config.get("network", True))},
            "shell_environment_policy.exclude": [*KEYS, self.provider["env_key"]],
            "web_search": "live" if web else "disabled",
            "check_for_update_on_startup": False,
        }
        argv = [self._codex(), "app-server"]
        for k, v in overrides.items():
            argv += ["-c", f"{k}={_toml(v)}"]
        for f in sorted(off):
            argv += ["--disable", f]
        return argv

    def _env(self, agent: str) -> dict:
        home = self.team_dir / "codex"
        home.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if k not in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN")}
        env.update({"CODEX_HOME": str(home), "ATEAM_AGENT": agent,
                    "ATEAM_TEAM_DIR": str(self.team_dir)})
        return env

    # --- one turn -----------------------------------------------------------

    def _call(self, srv: _Server, method: str, params: dict, deadline: float, on_message):
        """Send a request and wait for its answer, handling what comes first."""
        rid = srv.send(method, params)
        while True:
            m = srv.next(max(0.1, deadline - time.monotonic()))
            if m is None:
                tail = " | ".join(srv.stderr[-3:])
                raise RuntimeError(f"codex app-server exited{': ' + tail if tail else ''}")
            if m.get("id") == rid and "method" not in m:
                if "error" in m:
                    raise LookupError(str(m["error"].get("message") or m["error"]))
                return m.get("result") or {}
            on_message(m)

    def _answer(self, srv: _Server, m: dict) -> None:
        """A request from Codex. Nobody is there to approve anything."""
        method = m.get("method", "")
        if method.endswith("requestApproval") or method in ("applyPatchApproval", "execCommandApproval"):
            result = {"decision": "decline"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        else:
            srv.reply({"id": m["id"], "error": {"code": -32601,
                                                 "message": f"{method} is not available here"}})
            return
        srv.reply({"id": m["id"], "result": result})

    @staticmethod
    def _tool(item: dict) -> tuple[str, dict] | None:
        """A turn item as the tool call the rest of a-team knows it as."""
        kind = item.get("type")
        if kind == "commandExecution":
            return "run_command", {"command": item.get("command") or "", "cwd": item.get("cwd") or ""}
        if kind == "fileChange":
            return "edit_file", {"paths": [c.get("path") for c in item.get("changes") or []]}
        if kind == "mcpToolCall":
            server, tool = str(item.get("server") or ""), str(item.get("tool") or "")
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            return (tool if server.startswith("ateam_") else f"{server}.{tool}"), args
        if kind == "webSearch":
            return "search_web", {"query": item.get("query") or ""}
        if kind in ("dynamicToolCall", "collabAgentToolCall"):
            return str(item.get("tool") or kind), {}
        return None

    @staticmethod
    def _outcome(item: dict) -> tuple[str | None, str | None]:
        kind = item.get("type")
        if kind == "commandExecution":
            out = (item.get("aggregatedOutput") or "")[-4000:]
            failed = item.get("status") != "completed" or item.get("exitCode") not in (0, None)
            return (None, f"exit {item.get('exitCode')}: {out}") if failed else (out, None)
        if kind == "mcpToolCall":
            if item.get("error"):
                return None, str(item["error"].get("message") or item["error"])
            return json.dumps(item.get("result"), default=str)[:4000], None
        if kind == "fileChange" and item.get("status") not in ("completed", None):
            return None, str(item.get("status"))
        return "ok", None

    def _run(self, agent: str, message: str, cancel: threading.Event) -> str:
        t0 = time.monotonic()
        deadline = t0 + (self.timeout or 86400)
        spec, agents, spaces = self._spec(agent)
        model, effort = self.resolved_model(agent, self._model_for(agent, spec),
                                            spec.get("effort", self.effort))
        effort = effort or self.effort
        if effort not in EFFORT:
            return f"[error: unknown effort {effort!r}; use one of {', '.join(sorted(EFFORT))}]"
        cwd = spaces[0] if spaces else str(Path.cwd())
        cap = self._turn_tokens(spec)
        texts, started, usage = [], {}, {"base": None, "total": None, "last": None}
        state = {"turn": None, "thread": None, "done": None, "error": None, "capped": False,
                 "refusals": []}

        srv = _Server(self._argv(agent, spec, spaces), self._env(agent), cwd)

        def on_message(m: dict) -> None:
            method, p = m.get("method", ""), m.get("params") or {}
            if "id" in m and method:
                self._answer(srv, m)
            elif "id" in m:
                if "error" in m:            # an answer to turn/steer or turn/interrupt
                    print(f"    {agent}: codex refused a request: {m['error'].get('message')}",
                          flush=True)
            elif method == "item/started":
                call = self._tool(p.get("item") or {})
                if call:
                    started[p["item"]["id"]] = time.monotonic()
                    hook_pre_tool_use.audit(agent, call[0], call[1])
            elif method == "item/completed":
                item = p.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    texts.append(item["text"])
                call = self._tool(item)
                if call:
                    self._after(srv, agent, state, item, call, started.pop(item.get("id"), None))
            elif method == "thread/tokenUsage/updated":
                tu = p.get("tokenUsage") or {}
                total, last = tu.get("total") or {}, tu.get("last") or {}
                if usage["base"] is None:   # the thread's total before this turn
                    usage["base"] = {k: (total.get(k) or 0) - (last.get(k) or 0) for k in total}
                usage["total"], usage["last"] = total, last
                spent = self._spent(usage)
                if cap and spent and spent["fresh"] > cap and not state["capped"] and state["turn"]:
                    state["capped"] = True
                    srv.send("turn/interrupt", {"threadId": state["thread"], "turnId": state["turn"]})
            elif method == "account/rateLimits/updated":
                self._quota(model, p.get("rateLimits") or {})
            elif method == "error" and not p.get("willRetry"):
                state["error"] = p.get("error")
            elif method == "error":
                state["refusals"].append(json.dumps(p.get("error")))
            elif method == "turn/completed":
                state["done"] = p.get("turn") or {}

        try:
            self._call(srv, "initialize", {"clientInfo": {"name": "a-team", "title": None,
                                                          "version": _version()},
                                           "capabilities": None}, deadline, on_message)
            srv.send("initialized", notify=True)
            thread_params = {"model": model, "cwd": cwd, "sandbox": "workspace-write",
                             "approvalPolicy": "never",
                             "developerInstructions": persona.brief(agent, agents, self.scopes.shared_dir())}
            conv = self.conversation_id(agent)
            if conv:
                try:
                    res = self._call(srv, "thread/resume", {"threadId": conv, "excludeTurns": True,
                                                            **thread_params}, deadline, on_message)
                except LookupError as e:
                    # The thread is gone: a restored team, a cleared directory.
                    # The memories are what carry over; start a new one.
                    print(f"    {agent}: thread not found ({e}), starting a new one", flush=True)
                    self.reset(agent)
                    conv = None
            if not conv:
                res = self._call(srv, "thread/start", thread_params, deadline, on_message)
                conv = res["thread"]["id"]
                # Bound before the work is delivered (Runner.wake).
                self.remember_conversation(agent, conv)
            state["thread"] = conv
            res = self._call(srv, "turn/start", {
                "threadId": conv, "model": model, "effort": EFFORT[effort], "cwd": cwd,
                "input": [{"type": "text", "text": message, "text_elements": []}]},
                deadline, on_message)
            state["turn"] = (res.get("turn") or {}).get("id")
            interrupted = False
            while state["done"] is None:
                if (cancel.is_set() or time.monotonic() > deadline) and not interrupted:
                    interrupted = True
                    srv.send("turn/interrupt", {"threadId": conv, "turnId": state["turn"]})
                    deadline = time.monotonic() + 30
                    continue
                try:
                    m = srv.next(0.5)
                except queue.Empty:
                    if interrupted and time.monotonic() > deadline:
                        break
                    continue
                if m is None:
                    tail = " | ".join(srv.stderr[-3:])
                    return f"[error: codex app-server exited mid-turn{': ' + tail if tail else ''}]"
                on_message(m)
        except LookupError as e:
            return f"[error: codex: {e}]"
        except RuntimeError as e:
            if "unrecognized subcommand" in str(e):
                return (f"[error: {e}: this codex is too old for the runner; install 0.166 "
                        f"or later (npm install -g @openai/codex) or name one in \"codex\"]")
            return f"[error: {e}]"
        except queue.Empty:
            return f"[error: codex did not answer within {self.timeout:.0f}s]"
        finally:
            srv.close()

        turn = state["done"] or {}
        if cancel.is_set():
            return "[error: turn cancelled]"
        if state["capped"]:
            return self._limit_reached(agent, conv, model, t0, usage, cap)
        if turn.get("status") is None or (turn.get("status") == "interrupted" and not state["capped"]):
            # Not the word with_retry waits out: this turn had its time,
            # unless it spent it being refused for capacity.
            self._record(agent, conv, model, t0, usage)
            return (f"[error: the turn ran past {self.timeout:.0f}s"
                    f"{capacity_note(state['refusals'] + srv.stderr)}]")
        if turn.get("status") == "failed":
            return _error_text(turn.get("error") or state["error"])
        self._record(agent, conv, model, t0, usage)
        return "\n".join(texts)

    def _after(self, srv, agent, state, item, call, t0) -> None:
        """A finished call: on the record, counted, and any notes delivered."""
        tool, args = call
        result, error = self._outcome(item)
        try:
            self.observer.record_tool_call(
                agent=agent, conversation=state["thread"] or "", tool=tool, args=args,
                result=result, error=error,
                duration_s=max(0.0, time.monotonic() - t0) if t0 is not None else None)
        except Exception:
            pass                        # accounting must never break a turn
        steer.count_call(self.team_dir, agent, tool, args)
        note = steer.take_notes(self.team_dir, agent)
        if note and state["turn"]:
            # Mid-turn, as a message the model reads before its next step.
            srv.send("turn/steer", {"threadId": state["thread"], "expectedTurnId": state["turn"],
                                    "input": [{"type": "text", "text": note, "text_elements": []}]})

    def _quota(self, model: str, snap: dict) -> None:
        """The account's limits: a ChatGPT plan's two windows, as percent
        used, and a credit balance. A snapshot naming a model is that
        model's own limit. Updates are sparse: what is missing is unchanged."""
        applies = snap.get("normalModelSlug") or None
        for kind in ("primary", "secondary"):
            w = snap.get(kind)
            if isinstance(w, dict) and w.get("usedPercent") is not None:
                name = f"{snap.get('limitId') or ''}:{kind}".lstrip(":")
                self.note_quota(model, name, remaining=1 - w["usedPercent"] / 100,
                                resets_at=w.get("resetsAt"), applies=applies,
                                status="rejected" if w["usedPercent"] >= 100 else "allowed")
        credits = snap.get("credits")
        if isinstance(credits, dict) and credits.get("balance") is not None:
            self.note_quota(model, "", credits=credits["balance"])

    @staticmethod
    def _spent(usage: dict) -> dict | None:
        total, base = usage["total"], usage["base"] or {}
        if not total:
            return None
        d = {k: (total.get(k) or 0) - (base.get(k) or 0) for k in total}
        cached = d.get("cachedInputTokens") or 0
        fresh_in = (d.get("inputTokens") or 0) - cached
        return {"input": fresh_in, "cached": cached, "output": d.get("outputTokens") or 0,
                "fresh": fresh_in + (d.get("outputTokens") or 0)}

    def _record(self, agent: str, conv: str, model: str, t0: float, usage: dict) -> None:
        spent, last = self._spent(usage), usage["last"] or {}
        try:
            self.observer.record_turn(
                agent=agent, conversation=conv, duration_s=time.monotonic() - t0,
                input_tokens=spent and spent["input"], output_tokens=spent and spent["output"],
                cache_read_tokens=spent and spent["cached"],
                total_tokens=spent and spent["input"] + spent["output"] + spent["cached"],
                model=model, context_tokens=last.get("inputTokens") or None,
                # Codex reports tokens, not prices.
                cost_usd=None, **self.meta_for(agent))
        except Exception:
            pass                        # accounting must never break a turn

    def _limit_reached(self, agent, conv, model, t0, usage, cap) -> str:
        """Stopped by max_turn_tokens: not a failure, so not retried (see
        ClaudeRunner._limit_reached). The thread is kept; a reply continues."""
        self._record(agent, conv, model, t0, usage)
        what = f"limit of {cap:,} tokens"
        note = (f"I stopped: this turn reached its {what}. What you asked for is not "
                f"finished. Reply to have me continue, or raise the limit first.")
        try:
            bus = load_transport(agent)
            try:
                bus.send_kind("user", note, kind="blocker")
            finally:
                bus.close()
        except Exception:
            pass                        # the record still shows the turn
        return f"[stopped: {what}]"

    def wake(self, agent: str, message: str) -> str:
        t0 = time.monotonic()
        key = self.provider["env_key"]
        if not self._codex():
            out = (f"[error: {agent} cannot start: the codex CLI is not installed "
                   f"(npm install -g @openai/codex), or set \"codex\" in the runner config]")
        elif not os.environ.get(key):
            out = f"[error: {agent} cannot start: set {key} in the supervisor's environment]"
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
        """Interrupt the turn where it runs."""
        super().cancel(handle)
        live = self._live.get(handle.get("agent") or "")
        if live:
            live.set()


def _version() -> str:
    from . import __version__
    return __version__

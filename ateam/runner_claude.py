"""Runner that wakes agents on Claude, through the Claude Agent SDK.

    ATEAM_RUNNER=ateam.runner_claude:ClaudeRunner
    ANTHROPIC_API_KEY=...       # in the environment, never in the roster

or per agent, beside agents on other runtimes (runner_mixed):

    {"name": "qa", "runner": "ateam.runner_claude:ClaudeRunner",
     "model": "claude-opus-5-5", "effort": "high"}

Each turn is one `query()`: a process that starts, resumes the agent's session
from disk, works, and exits. A supervisor restart loses nothing, and there is
no long-lived session to keep healthy. Teammates are reached through the same
MCP servers the agy plugin mounts, so an agent has the same team tools on
every runtime.

An API key, or Bedrock, Vertex or Foundry, is required. Anthropic does not
allow a claude.ai login in a product built on this SDK. Sessions live under
<team_dir>/claude, apart from the operator's own ~/.claude, which also keeps
that login, and the operator's settings, out of an agent's process.

Requires claude-agent-sdk (`pip install "a-team[claude]"`), so this module
is excluded from the plugin's dependency-free module set.

Config (ATEAM_RUNNER_CONFIG), all optional:
    {"model": "claude-opus-5-5", "effort": "high", "workers": true,
     "timeout": 600, "max_turns": null, "max_budget_usd": null,
     "fallback_model": null, "setting_sources": [],
     "models": ["an-id-newer-than-this-file"]}
"""
import asyncio
import os
import sys
import time
import uuid
from urllib.parse import urlsplit
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKError,
                              HookMatcher, RateLimitEvent, ResultMessage, TextBlock, query)

from . import config as cfg
from . import hook_pre_tool_use
from . import persona
from . import policy
from . import roster as roster_lib
from . import scope
from . import steer
from .runner import Runner, with_retry
from .transport import load as load_transport

DEFAULT_MODEL = "claude-sonnet-5-5"
#: The manager and whoever reviews: judgement there is worth what it costs.
STRONG_MODEL = "claude-opus-5-5"
#: Dollars a turn may spend. "max_budget_usd": null in the config turns it off.
DEFAULT_TURN_BUDGET = 1.0
MODELS = [{"id": "claude-opus-5-5", "label": "Claude Opus 5.5"},
          {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
          {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5"},
          {"id": "claude-fable-5-1", "label": "Claude Fable 5.1"}]

# Any of these lets the CLI reach a model on terms the SDK allows.
CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
               "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")

# A turn the operator's own limits ended, by the CLI's name for each.
LIMITS = {"error_max_budget_usd": "${budget} budget", "error_max_turns": "limit of {turns} steps"}

# Effort as a roster writes it, for any runtime, to what this one takes.
EFFORT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high",
          "extra_high": "xhigh", "xhigh": "xhigh", "max": "max"}

# A roster's tools_off uses the names of the runtime it was written for, and a
# name this runtime does not know would withhold nothing, silently.
BUILTIN = {"run_command": ["Bash"], "view_file": ["Read"], "create_file": ["Write"],
           "edit_file": ["Edit", "NotebookEdit"], "list_directory": ["Glob"],
           "find_file": ["Glob"], "search_directory": ["Grep"],
           "invoke_subagent": ["Agent"], "search_web": ["WebSearch"],
           "read_url_content": ["WebFetch"]}

# Nobody is there to answer, and a question to the user goes over the bus.
WITHHELD = ["AskUserQuestion"]

# What an agent may call, and nothing else runs: an unattended agent has a
# fixed surface, and a call outside it is refused rather than left waiting for
# a prompt nobody will answer.
ALLOWED = ["Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep", "WebFetch",
           "WebSearch", "Agent", "TodoWrite", "mcp__ateam_memory__*", "mcp__ateam_bus__*",
           "mcp__ateam_tasks__*", "mcp__ateam_self__*"]

# Offering only these is most of what a turn costs. Left to itself the CLI sends
# every tool it has, each described in full on every request, and agents could
# call none outside ALLOWED: measured, 86% of a request, and 47,800 tokens of
# context for a turn that said one word.
BUILT_IN = [t for t in ALLOWED if not t.startswith("mcp__")]

# The keys that run the agents, unset at the start of each shell command. The
# CLI has a setting that scrubs them, but it also sandboxes Bash: package.json,
# node_modules, .env and .git/hooks go read-only, and with no network a coding
# agent cannot do its job. This keeps them from a stray `env`; it does not stop
# a determined agent, which runs as the same user. Keys a project's own tests
# need are left alone.
KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY")

# Which of a built-in tool's arguments name a path. The policy reads every
# short string as one, and "/usr/bin" in a grep pattern or an edit's old text
# is not a file the agent is touching.
PATH_ARGS = ("file_path", "path", "notebook_path")


def _strong(agent: str, spec: dict) -> bool:
    words = f"{agent} {spec.get('role') or ''}".lower()
    return bool(spec.get("is_principal") or spec.get("principal")) or any(
        w in words for w in ("manager", "faces outward", "review", "qa"))


def _gateway_env(gateway: dict, model: str) -> dict:
    """An Anthropic-shaped endpoint in place of Anthropic's: a vendor's own, or
    a router's. Anthropic does not support this for other vendors' models, so
    it is opt-in per agent, and a release of the CLI can break it."""
    url, var = str(gateway.get("base_url") or ""), str(gateway.get("token_env") or "")
    host = urlsplit(url).hostname or ""
    if not url.startswith("https://") and host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"gateway base_url must be https or loopback, got {url!r}")
    if not os.environ.get(var):
        raise ValueError(f"gateway token_env {var!r} is not set in the supervisor's environment")
    return {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": os.environ[var], "ANTHROPIC_API_KEY": "",
            # Pre-release request fields, which a provider that is not Anthropic rejects.
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            **{f"ANTHROPIC_DEFAULT_{tier}_MODEL": model for tier in ("OPUS", "SONNET", "HAIKU")}}


def _team_tool(name: str) -> str:
    """mcp__ateam_bus__send_to_teammate is the policy's send_to_teammate."""
    return name.split("__", 2)[2] if name.startswith("mcp__") and name.count("__") >= 2 else name


class ClaudeRunner(Runner):
    label = "claude"
    # Audit: the hooks in _hooks see every call, in the process that runs it.
    # Scoping: a disallowed tool's definition is removed from the request, so
    # the model never sees it. Containment is not provided: file tools are held
    # to an agent's workspaces by the policy, but Bash has the supervisor's
    # access. Run the team in a container, or withhold Bash with tools_off.
    supports_audit = True
    supports_containment = False
    supports_capability_scoping = True

    def __init__(self, config=None, observer=None):
        super().__init__(config, observer=observer)
        self.model = self.config.get("model")
        self.effort = self.config.get("effort", "high")
        self.timeout = self.config.get("timeout", cfg.TURN_TIMEOUT_S)
        self.use_workers = self.config.get("workers", True)
        self.scopes = scope.load()
        self._live: dict[str, tuple] = {}       # agent -> (loop, task) of its turn
        self._started: dict[str, float] = {}    # tool_use_id -> when it was called

    def _model_for(self, agent: str, spec: dict) -> str:
        """The roster's choice, then the team's, then one by role."""
        return spec.get("model") or self.model or (STRONG_MODEL if _strong(agent, spec) else DEFAULT_MODEL)

    def _gateway(self, spec: dict) -> dict | None:
        return spec.get("gateway") or self.config.get("gateway")

    def _budget(self, spec: dict) -> float | None:
        """Per turn. None behind a gateway: the CLI prices a model it does not
        know as if it were Claude, so that cap would stop turns at the wrong
        figure and read as if it were protecting anything."""
        if self._gateway(spec):
            return None
        return spec.get("max_budget_usd", self.config.get("max_budget_usd", DEFAULT_TURN_BUDGET))

    @classmethod
    def list_models(cls, config=None):
        """What this file knows of, plus the config's "models" for anything
        newer: naming one there lets a roster use it the day it ships."""
        extra = (config or {}).get("models") or []
        return MODELS + [{"id": m, "label": m} for m in extra]

    def _spec(self, agent: str) -> tuple[dict, list[dict], list[str]]:
        """(this agent's roster entry, every agent, the workspaces it has)."""
        roster = roster_lib.load(self.team_dir / "roster.json")
        spec = next((a for a in roster["agents"] if a["name"] == agent),
                    {"name": agent, "role": ""})
        spaces = list(dict.fromkeys([*roster.get("workspaces", []),
                                     *spec.get("workspaces", [])]))
        return spec, roster["agents"], spaces

    def _withheld(self, spec: dict) -> list[str]:
        off = list(WITHHELD)
        for name in spec.get("tools_off") or []:
            # A name with no built-in may be one of the team's own tools; a
            # deny rule takes a glob, so it goes for every server at once.
            off += BUILTIN.get(name) or [name, f"mcp__*__{name}"]
        if not (self.use_workers and spec.get("workers", True)):
            off.append("Agent")
        return list(dict.fromkeys(off))

    def _mcp_servers(self, agent: str) -> dict:
        """The servers the agy plugin mounts, with the same identity."""
        env = {"PYTHONPATH": str(cfg.PROJECT_ROOT), "ATEAM_AGENT": agent,
               "ATEAM_TEAM_DIR": str(self.team_dir)}
        for k in ("ATEAM_TEAM", "ATEAM_TEAMS_ROOT", "ATEAM_DURABLE_DIR",
                  "ATEAM_BUS_TRANSPORT", "ATEAM_BUS_CONFIG", "ATEAM_MEMORY_STORE",
                  "ATEAM_MEMORY_CONFIG"):
            if os.environ.get(k):
                env[k] = os.environ[k]
        workspace = str(self.scopes.agent_workspace(agent))
        return {"ateam_memory": {"command": sys.executable, "env": env,
                                   "args": ["-m", "ateam.mcp_memory", workspace]},
                **{f"ateam_{s}": {"command": sys.executable, "env": env,
                                    "args": ["-m", f"ateam.mcp_{s}"]}
                   for s in ("bus", "tasks", "self")}}

    def _hooks(self, agent: str) -> dict:
        """The team's policy and its record, on every tool call."""
        team_dir = self.team_dir
        gateway = self._gateway(self._spec(agent)[0]) or {}
        keys = " ".join(KEYS + ((gateway["token_env"],) if gateway.get("token_env") else ()))

        async def before(data, tool_use_id, context):
            raw, args = data["tool_name"], data.get("tool_input") or {}
            tool = _team_tool(raw)
            builtin = tool == raw and tool != "Bash"
            seen = {k: v for k, v in args.items() if k in PATH_ARGS or not builtin}
            seen.setdefault("cwd", data.get("cwd") or "")
            refusal = policy.check_tool_policy(agent, tool, seen, team_dir=team_dir)
            hook_pre_tool_use.audit(agent, tool, args, refusal or "")
            if refusal:
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "permissionDecision": "deny",
                                               "permissionDecisionReason": refusal}}
            if tool_use_id:
                self._started[tool_use_id] = time.monotonic()
            if tool == "Bash" and args.get("command"):
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "allow",
                    "updatedInput": {**args, "command": f"unset {keys}; {args['command']}"}}}
            return {}

        async def after(data, tool_use_id, context):
            t0 = self._started.pop(tool_use_id, None)
            try:
                self.observer.record_tool_call(
                    agent=agent, conversation=data.get("session_id") or "",
                    tool=_team_tool(data["tool_name"]), args=data.get("tool_input"),
                    result=None if "error" in data else str(data.get("tool_response")),
                    error=data.get("error"),
                    duration_s=max(0.0, time.monotonic() - t0) if t0 is not None else None)
            except Exception:
                pass                    # accounting must never break a turn
            steer.count_call(team_dir, agent, _team_tool(data["tool_name"]), data.get("tool_input"))
            note = steer.take_notes(team_dir, agent)
            if not note:
                return {}
            return {"hookSpecificOutput": {"hookEventName": data["hook_event_name"],
                                           "additionalContext": note}}

        return {"PreToolUse": [HookMatcher(hooks=[before])],
                "PostToolUse": [HookMatcher(hooks=[after])],
                "PostToolUseFailure": [HookMatcher(hooks=[after])]}

    def _options(self, agent: str, model: str, effort: str, session: str,
                 resume: bool) -> ClaudeAgentOptions:
        spec, agents, spaces = self._spec(agent)
        cwd = spaces[0] if spaces else str(Path.cwd())
        kept = {k: self.config[k] for k in ("max_turns", "fallback_model")
                if self.config.get(k) is not None}
        if self._budget(spec) is not None:
            kept["max_budget_usd"] = self._budget(spec)
        gateway = self._gateway(spec)
        if gateway:
            kept.pop("fallback_model", None)        # a Claude name means nothing there
        return ClaudeAgentOptions(
            model=model, effort=EFFORT[effort], cwd=cwd,
            add_dirs=[*spaces[1:], str(self.scopes.agent_workspace(agent)),
                      str(self.scopes.shared_dir())],
            # The brief is added to Claude's own system prompt: that is where
            # it learns its tools. The dynamic sections stay out so a session
            # keeps its cached prefix from one wake to the next.
            system_prompt={"type": "preset", "preset": "claude_code",
                           "exclude_dynamic_sections": True,
                           "append": persona.brief(agent, agents, self.scopes.shared_dir())},
            setting_sources=self.config.get("setting_sources", []), skills=[],
            mcp_servers=self._mcp_servers(agent), strict_mcp_config=True,
            permission_mode="dontAsk", allowed_tools=ALLOWED, tools=BUILT_IN,
            disallowed_tools=self._withheld(spec), hooks=self._hooks(agent),
            # The session is named here rather than learned from the result:
            # identity is bound before the work is delivered (Runner.wake).
            resume=session if resume else None, session_id=None if resume else session,
            env={"ATEAM_AGENT": agent, "ATEAM_TEAM_DIR": str(self.team_dir),
                 "CLAUDE_CONFIG_DIR": str(self.team_dir / "claude"),
                 # A supervisor started from a signed-in shell must not bring
                 # that login with it; empty counts as unset.
                 "CLAUDE_CODE_OAUTH_TOKEN": "",
                 # The brief names the team tools: they have to be there to call.
                 "ENABLE_TOOL_SEARCH": "false",
                 # An agent's memory is ateam's, read and reviewed there.
                 "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                 **(_gateway_env(gateway, model) if gateway else {})},
            **kept)

    def _record(self, agent: str, session: str, model: str, t0: float, result, last) -> None:
        usage, last = result.usage or {}, last or {}
        cached = usage.get("cache_read_input_tokens")
        fresh = None
        if usage.get("input_tokens") is not None:
            fresh = usage["input_tokens"] + (usage.get("cache_creation_input_tokens") or 0)
        out = usage.get("output_tokens")
        # usage adds up the requests of the turn; the last request's input is
        # how full the context is, which is what auto-cycle needs to know.
        context = None
        if last.get("input_tokens") is not None:
            context = sum(last.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens"))
        try:
            self.observer.record_turn(
                agent=agent, conversation=session, duration_s=time.monotonic() - t0,
                input_tokens=fresh, output_tokens=out, cache_read_tokens=cached,
                total_tokens=None if fresh is None else fresh + (out or 0) + (cached or 0),
                model=model, context_tokens=context,
                # The CLI prices by Claude's rates: wrong for anything else.
                cost_usd=None if self._gateway(self._spec(agent)[0]) else result.total_cost_usd,
                **self.meta_for(agent))
        except Exception:
            pass                        # accounting must never break a turn

    def _limit_reached(self, agent: str, session: str, model: str, t0: float, result,
                       last) -> str:
        """A turn stopped by max_budget_usd or max_turns.

        It is not a failure. Treated as one, it was retried, so a cap meant to
        stop spending kept spending, measured at four attempts in 75 seconds,
        and each attempt's cost went unrecorded. The turn ends here, its cost
        goes on the record, and the user is told, because the work is unfinished
        and nothing will wake the agent again. Its session is kept, so a reply
        continues where it stopped.
        """
        self._record(agent, session, model, t0, result, last)
        what = LIMITS[result.subtype].format(budget=self._budget(self._spec(agent)[0]),
                                             turns=self.config.get("max_turns"))
        spent = f" after spending ${result.total_cost_usd:.2f}" if result.total_cost_usd else ""
        note = (f"I stopped: this turn reached its {what}{spent}. What you asked for is "
                f"not finished. Reply to have me continue, or raise the limit first.")
        try:
            bus = load_transport(agent)
            try:
                bus.send_kind("user", note, kind="blocker")
            finally:
                bus.close()
        except Exception:
            pass                        # the record still shows the turn
        return f"[stopped: {what}]"

    async def _query(self, agent: str, message: str) -> str:
        t0 = time.monotonic()
        spec = self._spec(agent)[0]
        model, effort = self.resolved_model(agent, self._model_for(agent, spec),
                                            spec.get("effort", self.effort))
        session = self.conversation_id(agent)
        resume = bool(session)
        if not resume:
            session = str(uuid.uuid4())
            self.remember_conversation(agent, session)
        if (effort or self.effort) not in EFFORT:
            return f"[error: unknown effort {effort!r}; use one of {', '.join(sorted(EFFORT))}]"
        texts, result, last = [], None, None
        try:
            if self._gateway(spec):
                _gateway_env(self._gateway(spec), model)
        except ValueError as e:
            return f"[error: {e}]"
        try:
            async for m in query(prompt=message, options=self._options(
                    agent, model, effort or self.effort, session, resume)):
                if isinstance(m, AssistantMessage):
                    texts += [b.text for b in m.content if isinstance(b, TextBlock)]
                    last = m.usage or last
                elif isinstance(m, ResultMessage):
                    result = m
                elif isinstance(m, RateLimitEvent):
                    self._quota(model, m.rate_limit_info)
        except ClaudeSDKError as e:
            # A single query raises after yielding an error result, and the
            # result says more. Anything else never got as far as a result.
            if result is None or not result.is_error:
                return f"[error: {type(e).__name__}: {e}]"
        if result is None:
            return "[error: the turn ended with no result]"
        if result.subtype in LIMITS:
            return self._limit_reached(agent, session, model, t0, result, last)
        if result.is_error or result.stop_reason == "refusal":
            detail = "; ".join(result.errors or []) or result.result or result.stop_reason or ""
            status = f" {result.api_error_status}" if result.api_error_status else ""
            return f"[error: {result.subtype}{status}: {str(detail)[:500]}]"
        self._record(agent, session, model, t0, result, last)
        return "\n".join(texts) or str(result.result or "")

    def _quota(self, model: str, info) -> None:
        """A subscription limit, from the CLI: the five-hour and weekly
        windows hold for every model, the per-family weekly ones for theirs."""
        kind = getattr(info, "rate_limit_type", None) or "limit"
        used = getattr(info, "utilization", None)
        self.note_quota(model, kind, remaining=None if used is None else 1 - used,
                        resets_at=getattr(info, "resets_at", None),
                        status=getattr(info, "status", None),
                        applies={"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}.get(kind))

    async def _bounded(self, agent: str, message: str) -> str:
        """One turn, cancellable from another thread and held to the timeout."""
        self._live[agent] = (asyncio.get_running_loop(), asyncio.current_task())
        try:
            return await asyncio.wait_for(self._query(agent, message), self.timeout)
        except asyncio.TimeoutError:
            # Not the word with_retry waits out: this turn had its time.
            return f"[error: the turn ran past {self.timeout:.0f}s]"
        except asyncio.CancelledError:
            return "[error: turn cancelled]"
        finally:
            self._live.pop(agent, None)

    def _turn(self, agent: str, message: str) -> str:
        out = asyncio.run(self._bounded(agent, message))
        if out.startswith("[error:") and "no conversation found" in out.lower():
            # The session is gone: a restored team, a cleared directory. The
            # memories are what carry over; start a new one and say so.
            print(f"    {agent}: session not found, starting a new one", flush=True)
            self.reset(agent)
            out = asyncio.run(self._bounded(agent, message))
        return out

    def wake(self, agent: str, message: str) -> str:
        t0 = time.monotonic()
        if not self._gateway(self._spec(agent)[0]) and not any(os.environ.get(k) for k in CREDENTIALS):
            out = (f"[error: {agent} cannot start: set ANTHROPIC_API_KEY, or configure "
                   f"Bedrock, Vertex or Foundry, in the supervisor's environment]")
        else:
            def announce(attempt, delay, err):
                print(f"    {agent}: provider unavailable, retrying in {delay:.0f}s "
                      f"(attempt {attempt})", flush=True)
            try:
                out = with_retry(lambda: self._turn(agent, message), on_wait=announce)
            except Exception as e:
                out = f"[error: {agent} failed: {type(e).__name__}: {e}]"
        if out.startswith("[error:"):
            try:
                self.observer.record_failure(agent, self.conversation_id(agent) or "", out,
                                             duration_s=time.monotonic() - t0)
            except Exception:
                pass
        return out

    def cancel(self, handle: dict) -> None:
        """Stop the turn where it runs: ending the query ends its process."""
        super().cancel(handle)
        live = self._live.get(handle.get("agent") or "")
        if live:
            live[0].call_soon_threadsafe(live[1].cancel)

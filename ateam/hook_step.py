"""Step hooks for a host that runs a command around tool and model calls.

    python -m ateam.hook_step tool  < call.json     # after a tool call
    python -m ateam.hook_step model < step.json     # before a model call

The installed plugin wires these to agy's PostToolUse and PreInvocation, which
is how an agent on the CLI gets mail and repeat notes mid-turn (ateam.steer).
`tool` counts the call, puts it on the record where the team's runner does
not (a `tool_call` event, which is what triggers are learned from and what
Activity shows), and prints {}. `model` prints whatever is due as a message
in the conversation, or {} when nothing is:

    {"injectSteps": [{"systemMessage": {"systemMessage": "..."}}]}

A system message, because agy keeps it in the conversation; an ephemeral one
would be gone if the model's next step was spent on something else.

`tool` reads the call as ateam.hook_pre_tool_use does, and how it went from
whichever of these the host sends:

    result: "tool_response" | "tool_result" | "tool_output" | "result" |
            "response" | "output" (or camelCase): text, or an object, whose
            "error", "isError" or non-zero "exitCode" make it a failure
    error:  "error" | "tool_error" | "error_message" (or camelCase)
    time:   "duration_ms" | "durationMs" | "duration_s"

A host whose payload carries neither is recorded with the call alone, which
still says what the agent did and in what order.

The hooks run in a person's own agy sessions too, wherever the plugin is
installed, so anything not a supervised turn leaves at once. Exits 0 whatever
happens: a hook that fails must not fail the turn it is watching.
"""
import json
import os
import sys


def run(event: str, payload: dict) -> dict:
    from . import hook_pre_tool_use, scope, steer
    team_dir = scope.team_dir(os.environ.get("ATEAM_TEAM_DIR"))
    agent, tool, args = hook_pre_tool_use.parse(payload, team_dir)
    if not agent:
        return {}
    if event == "tool":
        if tool:
            record(team_dir, agent, tool, args, payload)
            steer.count_call(team_dir, agent, tool, args)
        return {}
    note = steer.take_notes(team_dir, agent)
    return {"injectSteps": [{"systemMessage": {"systemMessage": note}}]} if note else {}


RESULT_KEYS = ("tool_response", "toolResponse", "tool_result", "toolResult", "tool_output",
               "toolOutput", "result", "response", "output")
ERROR_KEYS = ("error", "tool_error", "toolError", "error_message", "errorMessage")
EXIT_KEYS = ("exitCode", "exit_code", "ExitCode", "returncode")


def _say(v) -> str:
    """What a host sent as a result or an error, as text: a string, an MCP
    result's text parts, an error object's message, or the JSON of it."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        parts = v.get("content")
        if isinstance(parts, list):
            text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
            if text:
                return text
        for k in ("message", "stderr", "text", "output", "stdout"):
            if isinstance(v.get(k), str) and v[k]:
                return v[k]
    return json.dumps(v, default=str)


def outcome(payload: dict) -> tuple[str | None, str | None]:
    """(result, error) of the call this payload reports; one of them None,
    both when the host says nothing of how it went."""
    call = payload.get("toolCall") or payload.get("tool_call")
    if isinstance(call, dict):
        payload = {**call, **payload}
    error = next((payload[k] for k in ERROR_KEYS if payload.get(k)), None)
    if error is not None:
        return None, _say(error)
    res = next((payload[k] for k in RESULT_KEYS if payload.get(k) is not None), None)
    if res is None:
        return None, None
    if isinstance(res, dict):
        inner = next((res[k] for k in ERROR_KEYS if res.get(k)), None)
        if inner is not None:
            return None, _say(inner)
        code = next((res[k] for k in EXIT_KEYS if isinstance(res.get(k), int)
                     and not isinstance(res.get(k), bool)), 0)
        if code:
            return None, f"exit {code}: {_say(res)}"
        if res.get("isError") or res.get("is_error"):
            return None, _say(res)
    return _say(res), None


def record(team_dir, agent: str, tool: str, args: dict, payload: dict) -> None:
    """Put the call on the record, in a supervised turn whose runner does
    not record its calls itself. Never raises."""
    try:
        from . import observer, steer
        turn = steer.turn_of(team_dir, agent)
        if not turn or turn.get("calls"):
            return
        result, error = outcome(payload)
        ms = next((payload[k] for k in ("duration_ms", "durationMs")
                   if isinstance(payload.get(k), (int, float))), None)
        secs = ms / 1000 if ms is not None else payload.get("duration_s")
        conv = next((str(payload[k]) for k in ("conversation_id", "conversationId", "session_id",
                                               "sessionId", "conversation") if payload.get(k)), "")
        obs = observer.load(config={"team_dir": str(team_dir)}) \
            if not os.environ.get("ATEAM_OBSERVER_CONFIG") else observer.load()
        obs.record_tool_call(agent=agent, conversation=conv, tool=tool, args=args,
                             result=result, error=error,
                             duration_s=secs if isinstance(secs, (int, float)) else None)
    except (Exception, SystemExit) as e:    # an observer that will not load exits
        print(f"[hook_step: call not recorded: {type(e).__name__}: {e}]", file=sys.stderr)


def main() -> int:
    out = {}
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict) and sys.argv[1:] and sys.argv[1] in ("tool", "model"):
            out = run(sys.argv[1], payload)
    except Exception as e:
        print(f"[hook_step: {type(e).__name__}: {e}]", file=sys.stderr)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

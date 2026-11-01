"""Step hooks for a host that runs a command around tool and model calls.

    python -m agyteam.hook_step tool  < call.json     # after a tool call
    python -m agyteam.hook_step model < step.json     # before a model call

The installed plugin wires these to agy's PostToolUse and PreInvocation, which
is how an agent on the CLI gets mail and repeat notes mid-turn (agyteam.steer).
`tool` counts the call and prints {}. `model` prints whatever is due as a
message in the conversation, or {} when nothing is:

    {"injectSteps": [{"systemMessage": {"systemMessage": "..."}}]}

A system message, because agy keeps it in the conversation; an ephemeral one
would be gone if the model's next step was spent on something else.

The hooks run in a person's own agy sessions too, wherever the plugin is
installed, so anything not a supervised turn leaves at once. Exits 0 whatever
happens: a hook that fails must not fail the turn it is watching.
"""
import json
import os
import sys


def run(event: str, payload: dict) -> dict:
    from . import hook_pre_tool_use, scope, steer
    team_dir = scope.team_dir(os.environ.get("AGYTEAM_TEAM_DIR"))
    agent, tool, args = hook_pre_tool_use.parse(payload, team_dir)
    if not agent:
        return {}
    if event == "tool":
        if tool:
            steer.count_call(team_dir, agent, tool, args)
        return {}
    note = steer.take_notes(team_dir, agent)
    return {"injectSteps": [{"systemMessage": {"systemMessage": note}}]} if note else {}


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

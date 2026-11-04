"""A scripted ACP agent, for testing runner_acp without a model.

    python evals/fake_acp_agent.py      # JSON-RPC lines on stdin/stdout

It keeps its sessions, and everything it was asked, in FAKE_ACP_STATE (a
JSON file), so a session outlives the process as a real harness's does.
A prompt is acted on by words in it:

    "run: <command>"   asks permission to run <command>, then reports the answer
    "team-tool"        reports a call to ateam_bus_send_to_teammate
    "fail429"          answers the prompt with a 429 error (on model FAKE_ACP_FULL)
Anything else is echoed back. Every turn reports usage and a running cost.
"""
import json
import os
import sys

STATE = os.environ["FAKE_ACP_STATE"]
MODELS = ["big", "small", "medium"]


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"sessions": {}, "log": []}


def save(st):
    with open(STATE, "w") as f:
        json.dump(st, f)


def out(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def update(sid, u):
    out({"method": "session/update", "params": {"sessionId": sid, "update": u}})


def options(model):
    return [{"id": "model", "name": "Model", "category": "model", "type": "select",
             "currentValue": model, "options": [{"value": m, "name": m.title()} for m in MODELS]}]


def ask(sid, call, nid):
    out({"id": nid, "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": call,
        "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "reject", "kind": "reject_once", "name": "Reject"}]}})
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("id") == nid and "method" not in m:
            return m["result"]["outcome"].get("optionId")
    return None


def main():
    st = load()
    nid = 1000
    for line in sys.stdin:
        m = json.loads(line)
        method, p, i = m.get("method"), m.get("params") or {}, m.get("id")
        st["log"].append({"method": method, "params": p})
        save(st)
        if method == "initialize":
            out({"id": i, "result": {"protocolVersion": 1, "agentCapabilities": {
                "loadSession": True, "sessionCapabilities": {"resume": {}, "close": {}}}}})
        elif method == "session/new":
            sid = f"ses_{len(st['sessions']) + 1}"
            st["sessions"][sid] = {"model": "big", "turns": 0, "cost": 0.0,
                                   "mcp": [s["name"] for s in p.get("mcpServers") or []]}
            save(st)
            out({"id": i, "result": {"sessionId": sid, "configOptions": options("big")}})
        elif method in ("session/resume", "session/load"):
            s = st["sessions"].get(p.get("sessionId"))
            if s is None:
                out({"id": i, "error": {"code": -32002, "message": "Session not found"}})
            else:
                out({"id": i, "result": {"configOptions": options(s["model"])}})
        elif method == "session/set_config_option":
            st["sessions"][p["sessionId"]]["model"] = p["value"]
            save(st)
            out({"id": i, "result": {"configOptions": options(p["value"])}})
        elif method == "session/close":
            out({"id": i, "result": {}})
        elif method == "session/prompt":
            sid = p["sessionId"]
            s = st["sessions"][sid]
            text = "".join(b.get("text", "") for b in p.get("prompt") or [])
            if "fail429" in text and s["model"] == os.environ.get("FAKE_ACP_FULL", "big"):
                out({"id": i, "error": {"code": -32603,
                                        "message": "429 Too Many Requests: rate limit for " + s["model"]}})
                continue
            reply = f"[{s['model']}] heard: {text[-80:]}"
            if "run: " in text:
                cmd = text.split("run: ", 1)[1].splitlines()[0].strip()
                call = {"toolCallId": "t1", "title": "bash", "kind": "execute", "status": "pending",
                        "rawInput": {"command": cmd}}
                update(sid, {"sessionUpdate": "tool_call", **call})
                nid += 1
                answer = ask(sid, call, nid)
                if answer == "once":
                    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                                 "status": "in_progress"})
                    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                                 "status": "completed",
                                 "content": [{"type": "content", "content": {"type": "text", "text": "ran"}}]})
                else:
                    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                                 "status": "failed",
                                 "content": [{"type": "content", "content": {"type": "text", "text": "rejected"}}]})
                reply = f"permission: {answer}"
            if "team-tool" in text:
                update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t2",
                             "title": "ateam_bus_send_to_teammate", "kind": "other", "status": "pending",
                             "rawInput": {"to": "qa", "content": "hi"}})
                update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t2",
                             "status": "in_progress"})
                update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t2",
                             "status": "completed",
                             "content": [{"type": "content", "content": {"type": "text", "text": "[delivered to qa]"}}]})
            s["turns"] += 1
            s["cost"] = round(s["cost"] + 0.01, 6)
            save(st)
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply}})
            update(sid, {"sessionUpdate": "usage_update", "used": 1000 * s["turns"], "size": 100000,
                         "cost": {"amount": s["cost"], "currency": "USD"}})
            out({"id": i, "result": {"stopReason": "end_turn", "usage": {
                "inputTokens": 100, "outputTokens": 10, "totalTokens": 610, "cachedReadTokens": 500}}})
        elif i is not None:
            out({"id": i, "error": {"code": -32601, "message": f"no {method}"}})


if __name__ == "__main__":
    main()

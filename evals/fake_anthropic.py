"""A local stand-in for the Messages API, so the real Claude CLI runs offline.

The Agent SDK is a library around a CLI, and what a runner has to get right
lives between them: flags it accepts, sessions it resumes, tools and hooks it
really mounts. A faked SDK would pass while all of that was wrong. So the tests
run the real CLI and fake only the far end, with no key and no cost.

    api = FakeAnthropic(["hello", use("mcp__ateam_bus__list_teammates", {}), "done"])
    ANTHROPIC_BASE_URL=api.url  ANTHROPIC_API_KEY=not-a-real-key

Each entry answers one request that offered tools, in order, and the last is
repeated. A request with no tools is the CLI's own housekeeping: it gets "ok"
and is not recorded. `api.requests` is every recorded request body, and
`api.headers` the headers of the latest request.
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def use(name, args=None, text=""):
    """A scripted reply that calls a tool."""
    return {"tool": name, "args": args or {}, "text": text}


def status(code, message="overloaded"):
    """A scripted reply that fails."""
    return {"status": code, "message": message}


def _blocks(reply):
    if isinstance(reply, str):
        return [{"type": "text", "text": reply}], "end_turn"
    out = [{"type": "text", "text": reply["text"]}] if reply.get("text") else []
    out.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}",
                "name": reply["tool"], "input": reply["args"]})
    return out, "tool_use"


class FakeAnthropic:
    def __init__(self, script, usage=None):
        self.script, self.requests, self.housekeeping = list(script), [], []
        self.usage = usage or {"input_tokens": 100, "output_tokens": 10,
                               "cache_read_input_tokens": 1000,
                               "cache_creation_input_tokens": 20}
        self.hold = None                # an Event: replies wait on it when set
        self.lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, doc):
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json(200, {"data": [], "has_more": False})

            def do_HEAD(self):
                self._json(200, {})

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                if "/v1/messages" not in self.path or "count_tokens" in self.path:
                    return self._json(200, {"input_tokens": 100})
                api.headers = dict(self.headers.items())
                reply = api.next_reply(body)
                if isinstance(reply, dict) and "status" in reply:
                    return self._json(reply["status"], {
                        "type": "error",
                        "error": {"type": "api_error", "message": reply["message"]}})
                if api.hold is not None and body.get("tools"):
                    api.hold.wait(30)
                blocks, stop = _blocks(reply)
                msg = {"id": f"msg_{uuid.uuid4().hex[:20]}", "type": "message",
                       "role": "assistant", "model": body.get("model"), "stop_sequence": None}
                if not body.get("stream"):
                    return self._json(200, {**msg, "content": blocks, "stop_reason": stop,
                                            "usage": api.usage})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def event(name, data):
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                event("message_start", {"type": "message_start", "message": {
                    **msg, "content": [], "stop_reason": None, "usage": api.usage}})
                for i, b in enumerate(blocks):
                    start = {**b, "text": ""} if b["type"] == "text" else {**b, "input": {}}
                    event("content_block_start", {"type": "content_block_start", "index": i,
                                                  "content_block": start})
                    delta = ({"type": "text_delta", "text": b["text"]} if b["type"] == "text"
                             else {"type": "input_json_delta", "partial_json": json.dumps(b["input"])})
                    event("content_block_delta", {"type": "content_block_delta", "index": i,
                                                  "delta": delta})
                    event("content_block_stop", {"type": "content_block_stop", "index": i})
                event("message_delta", {"type": "message_delta",
                                        "delta": {"stop_reason": stop, "stop_sequence": None},
                                        "usage": {"output_tokens": api.usage["output_tokens"]}})
                event("message_stop", {"type": "message_stop"})

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._http.server_address[1]}"
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def next_reply(self, body):
        with self.lock:
            if not body.get("tools"):
                self.housekeeping.append(body)
                return "ok"
            self.requests.append(body)
            return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def close(self):
        self._http.shutdown()
        self._http.server_close()

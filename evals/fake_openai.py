"""A local stand-in for the Responses API, so the real Codex CLI runs offline.

As with fake_anthropic.py: what a runner has to get right lives between it and
Codex (flags, threads it resumes, tools and MCP servers it really mounts), so
the tests run the real `codex app-server` and fake only the far end.

    api = FakeOpenAI(["hello", call("ateam_bus__list_teammates", {}), "done"])
    provider base_url = api.url + "/v1"

Each entry answers one request, in order, and the last is repeated. A reply
naming a tool calls it; `api.requests` is every request body.
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def call(name, args=None, text=""):
    """A scripted reply that calls a tool: a function with `args` as a dict, or
    a freeform tool, such as code mode's `exec`, with `args` as its text."""
    return {"tool": name, "args": {} if args is None else args, "text": text}


def js(source, text=""):
    """Code mode: the model runs JavaScript that calls the nested tools."""
    return call("exec", source, text)


def status(code, message="overloaded", kind="server_error"):
    """A scripted reply that fails."""
    return {"status": code, "message": message, "kind": kind}


class FakeOpenAI:
    def __init__(self, script, usage=None):
        self.script, self.requests = list(script), []
        self.usage = usage or {"input_tokens": 1100, "output_tokens": 10, "total_tokens": 1110,
                               "input_tokens_details": {"cached_tokens": 1000},
                               "output_tokens_details": {"reasoning_tokens": 0}}
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
                self._json(200, {"object": "list", "data": [], "models": []})

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) or b"{}"
                if self.headers.get("Content-Encoding") == "zstd":
                    return self._json(415, {"error": {"message": "zstd not supported here"}})
                body = json.loads(raw)
                if not self.path.rstrip("/").endswith("/responses"):
                    return self._json(404, {"error": {"message": f"no {self.path}"}})
                api.headers = dict(self.headers.items())
                reply = api.next_reply(body)
                if isinstance(reply, dict) and "status" in reply:
                    return self._json(reply["status"], {"error": {
                        "message": reply["message"], "type": reply["kind"], "code": reply["kind"]}})
                if api.hold is not None:
                    api.hold.wait(30)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                rid = f"resp_{uuid.uuid4().hex[:20]}"
                seq = [0]

                def event(name, data):
                    seq[0] += 1
                    data = {"type": name, "sequence_number": seq[0], **data}
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                base = {"id": rid, "object": "response", "model": body.get("model"),
                        "status": "in_progress", "output": []}
                event("response.created", {"response": base})
                items = []
                text = reply if isinstance(reply, str) else reply.get("text")
                if text:
                    items.append({"type": "message", "id": f"msg_{uuid.uuid4().hex[:16]}",
                                  "role": "assistant", "status": "completed",
                                  "content": [{"type": "output_text", "text": text,
                                               "annotations": []}]})
                if isinstance(reply, dict) and isinstance(reply.get("args"), str):
                    items.append({"type": "custom_tool_call", "id": f"ctc_{uuid.uuid4().hex[:16]}",
                                  "call_id": f"call_{uuid.uuid4().hex[:16]}", "status": "completed",
                                  "name": reply["tool"], "input": reply["args"]})
                elif isinstance(reply, dict) and reply.get("tool"):
                    items.append({"type": "function_call", "id": f"fc_{uuid.uuid4().hex[:16]}",
                                  "call_id": f"call_{uuid.uuid4().hex[:16]}", "status": "completed",
                                  "name": reply["tool"], "arguments": json.dumps(reply["args"])})
                for i, item in enumerate(items):
                    event("response.output_item.added", {"output_index": i, "item": item})
                    if item["type"] == "message":
                        event("response.output_text.delta", {"output_index": i, "content_index": 0,
                                                             "item_id": item["id"], "delta": text})
                    event("response.output_item.done", {"output_index": i, "item": item})
                event("response.completed", {"response": {**base, "status": "completed",
                                                          "output": items, "usage": api.usage}})

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._http.server_address[1]}"
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def next_reply(self, body):
        with self.lock:
            self.requests.append(body)
            return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def close(self):
        self._http.shutdown()
        self._http.server_close()

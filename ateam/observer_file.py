"""File-backed observer.

Default implementation of the observer seam. Records turn completions,
failures, and episodes as JSON lines in <team_dir>/events.jsonl.
Also maintains <team_dir>/usage.jsonl for backward compatibility with
mcp_self.py:my_activity so agents can introspect their own usage history.

Standard library only. Never raises on write failures: accounting must never
break a turn or supervisor loop.
"""
import json
import os
import time
from pathlib import Path

from . import logtail
from .observer import Observer

from . import config


def clip(text: str, limit: int) -> str:
    """`text` in about `limit` characters: its start and its end, with how
    much between them was left out. The end is kept because that is where
    a command says how it went."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 3 // 4
    return (text[:head] + f"\n...[{len(text) - limit} chars left out]...\n"
            + text[len(text) - (limit - head):])


def _slim(value, limit: int, depth: int = 0):
    """A call's arguments with each long string clipped: a file written
    whole is not kept whole in the log as well."""
    if isinstance(value, str):
        return clip(value, limit)
    if depth < 6 and isinstance(value, dict):
        return {k: _slim(v, limit, depth + 1) for k, v in value.items()}
    if depth < 6 and isinstance(value, (list, tuple)):
        return [_slim(v, limit, depth + 1) for v in value]
    return value


class FileObserver(Observer):
    """File-backed observer writing JSONL records to the team directory."""

    label = "file"

    def __init__(self, config: dict | None = None):
        super().__init__(config)

    @property
    def team_dir(self) -> Path:
        if "team_dir" in self.config:
            return Path(self.config["team_dir"])
        env = os.environ.get("ATEAM_TEAM_DIR")
        if env:
            return Path(env)
        from . import scope
        return scope.load().team_dir()

    @property
    def events_path(self) -> Path:
        return self.team_dir / "events.jsonl"

    @property
    def usage_path(self) -> Path:
        return self.team_dir / "usage.jsonl"

    def _write_event(self, record: dict) -> None:
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass  # accounting must never break a turn

    def record_turn(self, agent: str, conversation: str, duration_s: float,
                    input_tokens: int | None = None,
                    output_tokens: int | None = None,
                    cache_read_tokens: int | None = None,
                    total_tokens: int | None = None,
                    model: str | None = None,
                    **kwargs) -> None:
        event = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": "turn",
            "agent": agent,
            "conversation": conversation,
            "duration_s": round(duration_s, 2) if duration_s is not None else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "total_tokens": total_tokens,
            "model": model,
            **kwargs,
        }
        self._write_event(event)

        # Mirror turn usage to usage.jsonl so mcp_self:my_activity continues
        # to reflect turns across both AgyRunner and SdkRunner without modifying mcp_self.
        usage_entry = {
            "ts": event["ts"],
            "agent": agent,
            "conversation": conversation,
            "duration_s": event["duration_s"] if event["duration_s"] is not None else 0.0,
        }
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens", "model"):
            v = event.get(k)
            if v is not None:
                usage_entry[k] = v
        try:
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            with self.usage_path.open("a") as f:
                f.write(json.dumps(usage_entry, default=str) + "\n")
        except Exception:
            pass

    def record_tool_call(self, agent: str, conversation: str, tool: str,
                         args: dict | None = None,
                         result: str | None = None,
                         error: str | None = None,
                         duration_s: float | None = None,
                         **kwargs) -> None:
        try:
            if result is not None and not isinstance(result, str):
                try:
                    result = json.dumps(result, default=str)
                except Exception:
                    result = str(result)
            event = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": "tool_call",
                "agent": agent,
                "conversation": conversation,
                "tool": tool,
                "args": _slim(args, config.TOOL_ARG_LOG_CHARS),
                "result": clip(result, config.TOOL_RESULT_LOG_CHARS) if result is not None else None,
                "error": clip(str(error), config.TOOL_ERROR_LOG_CHARS) if error is not None else None,
                "duration_s": round(duration_s, 2) if duration_s is not None else None,
                **kwargs,
            }
            self._write_event(event)
        except Exception:
            pass

    def record_failure(self, agent: str, conversation: str, error: str,
                       duration_s: float | None = None, **kwargs) -> None:
        event = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": "failure",
            "agent": agent,
            "conversation": conversation,
            "error": error,
            "duration_s": round(duration_s, 2) if duration_s is not None else None,
            **kwargs,
        }
        self._write_event(event)

    def record_episode(self, turns: int, stopped_reason: str,
                       reviewed: bool = False,
                       duration_s: float | None = None, **kwargs) -> None:
        event = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": "episode",
            "turns": turns,
            "stopped_reason": stopped_reason,
            "reviewed": reviewed,
            "duration_s": round(duration_s, 2) if duration_s is not None else None,
            **kwargs,
        }
        self._write_event(event)

    def record_event(self, event: str, **fields) -> None:
        self._write_event({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "event": event, **fields})

    def events(self, event_type: str | None = None) -> list[dict]:
        """Return recorded events, or [] if missing or empty.

        One kind of event (other than tool calls) costs only what was
        appended since it was last asked for: the supervisor asks for the
        turns on every pass, and read whole each time the log's size became
        the length of a pass. Everything, or the tool calls, still reads
        the whole file; for one agent's recent calls, calls()."""
        try:
            if event_type is not None and event_type != "tool_call":
                rows, _ = logtail.tail(self.events_path, without="tool_call")
                return [dict(r) for r in rows if r.get("event") == event_type]
            if not self.events_path.exists():
                return []
            records = []
            for line in self.events_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if event_type is None or rec.get("event") == event_type:
                        records.append(rec)
                except json.JSONDecodeError:
                    continue
            return records
        except OSError:
            return []

    def calls(self, agent: str, since: str = "") -> list[dict]:
        """The agent's tool calls from `since`, oldest first, read back from
        the end of the log no further than `since`."""
        out, older = [], 0
        try:
            for row in logtail.newest(self.events_path):
                ts = str(row.get("ts") or "")
                if since and ts and ts < since:
                    # Several processes append, each stamping before it
                    # writes: one older line is not yet the end of them.
                    older += 1
                    if older > 64:
                        break
                    continue
                if ts:
                    older = 0
                if row.get("event") == "tool_call" and row.get("agent") == agent:
                    out.append(row)
        except OSError:
            pass
        return out[::-1]

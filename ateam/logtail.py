"""Read an append-only JSONL log without reading it all again.

A team's logs only grow, and they are read far more often than they are
written: the app reads them for every refresh of every open tab, the
supervisor on every pass. Parsed whole each time, a log's size became
everyone's wait; at 200 MB of events one refresh took three seconds and six
at once fourteen.

    rows, sig = tail(path)                  # parsed once; later only what was appended
    rows, sig = tail(path, without="tool_call")     # leaving one kind of event out
    for row in newest(path): ...            # from the end, as far back as the caller reads

Tool calls are most of an event log by weight (each carries what the tool
returned) and almost nothing reads them, so the readers that do not leave
them out before parsing a byte of them.
"""
import itertools
import json
import threading
from pathlib import Path

# One entry per (path, kind left out). The rows are kept for the life of the
# process, which is why a reader that has no use for tool calls must not
# keep them.
_TAILS: dict[tuple, dict] = {}
_LOCK = threading.Lock()
_GEN = itertools.count()                # a log read anew never repeats a signature

#: How far into a line the event's kind is looked for before parsing it:
#: FileObserver writes {"ts": ..., "event": ...} first. A line that carries
#: it anywhere else is parsed and then left out, so this is only a saving.
HEAD = 96
BLOCK = 8 << 20


def _marker(kind: str) -> bytes:
    return b'"event": ' + json.dumps(kind).encode()


def tail(path, without: str | None = None) -> tuple[list[dict], tuple]:
    """The rows of an append-only JSONL log, and a signature that changes
    whenever they do. With `without`, events of that kind are left out, and
    the signature does not change when only they are appended.

    The rows are shared between callers: do not change them. A log that
    shrank or was replaced is read again from the start; a line still being
    written is left for the next read."""
    path = Path(path)
    key = (str(path), without)
    try:
        st = path.stat()
    except OSError:
        with _LOCK:
            _TAILS.pop(key, None)
        return [], ()
    skip = _marker(without) if without else None
    with _LOCK:
        t = _TAILS.get(key)
        if t is None or t["ino"] != (st.st_dev, st.st_ino) or st.st_size < t["offset"]:
            t = _TAILS[key] = _fresh((st.st_dev, st.st_ino))
        if st.st_size > t["offset"]:
            new = []
            with path.open("rb") as f:
                # The bytes before where it left off, read again: a file
                # rewritten in place rather than appended to is read anew.
                f.seek(t["offset"] - len(t["edge"]))
                if f.read(len(t["edge"])) != t["edge"]:
                    t = _TAILS[key] = _fresh(t["ino"])
                    f.seek(0)
                offset, edge, carry = t["offset"], t["edge"], b""
                left = st.st_size - offset
                while left > 0:
                    # A block at a time: a first read of a large log must
                    # not hold all of it at once.
                    data = f.read(min(BLOCK, left))
                    if not data:
                        break
                    left -= len(data)
                    chunk = carry + data
                    end = chunk.rfind(b"\n") + 1
                    carry = chunk[end:]
                    for line in chunk[:end].split(b"\n"):
                        if not line or (skip and line.find(skip, 0, HEAD) != -1):
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and not (without and row.get("event") == without):
                            new.append(row)
                    offset += end
                    edge = (edge + chunk[max(0, end - 256):end])[-256:]
            t.update(offset=offset, edge=edge)
            if new:
                # A new list, so one a caller is reading does not grow under it.
                t["rows"] = t["rows"] + new
        return t["rows"], (key, t["ino"], t["gen"], len(t["rows"]))


def _fresh(ino) -> dict:
    return {"ino": ino, "offset": 0, "rows": [], "edge": b"", "gen": next(_GEN)}


def forget(path=None) -> None:
    """Drop what is remembered of `path` (or of every log)."""
    with _LOCK:
        for key in [k for k in _TAILS if path is None or k[0] == str(path)]:
            del _TAILS[key]


def newest(path, block: int = 1 << 20, contains: bytes | None = None):
    """The log's rows from the last to the first, read from the end a block
    at a time: a caller that wants the recent ones stops early and the rest
    of the file is never read. With `contains`, only lines carrying those
    bytes are parsed."""
    try:
        f = Path(path).open("rb")
    except OSError:
        return
    with f:
        f.seek(0, 2)
        pos, carry = f.tell(), b""
        while pos > 0:
            n = min(block, pos)
            pos -= n
            f.seek(pos)
            lines = (f.read(n) + carry).split(b"\n")
            carry = lines[0]            # its start is in the block before
            for line in reversed(lines[1:]):
                row = _row(line, contains)
                if row is not None:
                    yield row
        row = _row(carry, contains)
        if row is not None:
            yield row


def _row(line: bytes, contains: bytes | None) -> dict | None:
    if not line or (contains and contains not in line):
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None                     # a line still being written, or damage
    return row if isinstance(row, dict) else None

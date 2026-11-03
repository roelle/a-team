"""Fast recall: a scorer that ranks an agent's memories with no model call,
trained by the judge and by what happened after.

Recall has a slow path: candidates, then a model choosing among them. It is
careful, and it costs a call a wake that a turn may wait on. This is the fast
path beside it: a few signals per memory, weighed by weights it learns, so
that the next wake can be answered at once and the judge's answer can follow.

It is not a network over all memories. Each memory keeps its own evidence --
requests it applied to and requests it did not ("prototypes", kept as small
vectors), and how often it was chosen when it was a candidate -- so a new
memory does not disturb what the others learned. Only six weights are
shared. That keeps it stable as memories pile up:

    every list of prototypes has a fixed size: when full, the two closest
    merge, so evidence is summarised, never dropped and never unbounded;
    each update is smaller than the last (the step falls with the number of
    updates), and the weights are pulled back toward where they started;
    the judge labels only the candidates it sees, so some are added for it
    (`fast_explore`): the fast path's top pick, and the memory the judge has
    graded least for its score. Its top picks alone would teach it only
    about what it already ranks high, and never show it its blind spots.

Requests are compared after taking away the average request. Most of what
any two requests share -- "can you look at", the team's own vocabulary --
says nothing about which memory applies, and embeddings are known to crowd
into one cone; centring is what lets evidence tell memories apart.

Labels, strongest last: the judge's choice among the candidates (each wake);
a memory the agent opened itself, unoffered; the used-check's verdict; a
correction that restated a memory (a miss, or one it was shown and ignored).

`"fast": "shadow"` (the default when recall is on) runs it, learns and
records what it would have offered, and offers nothing: the judge still
decides what an agent sees. bench/recall/fast_report.py reads that record:
how often the fast path agreed with the judge, by day, and a replay of the
record from scratch to test a change to this file against real requests.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
from pathlib import Path

#: Signals, in the order of the weight vector.
FEATURES = ("sim", "words", "pos", "neg", "prior")
#: Where the weights start: the meaning of the request and the memory, then
#: its words, then its evidence. Learning moves them; L2 pulls them back.
START = {"bias": -4.0, "sim": 5.0, "words": 2.0, "pos": 4.0, "neg": -4.0, "prior": 1.0}
PROTOS = 4                 # prototypes kept per memory and side
PROTO_DIMS = 96            # prototypes are projections, so they stay small
HASH_DIMS = 256            # the bag-of-words space when there is no embedder
THRESHOLD = 0.5
STEP = 0.2                 # the first update's size; it falls as 1/sqrt(n)
PULL = 0.002               # L2 toward START
MEAN_RATE = 0.02           # how fast the average request moves, once settled
CLIP = 20.0
JUDGE_W, FOUND_W, USED_W, MISS_W = 1.0, 1.0, 1.5, 2.0


def _terms(text):
    from .recall import terms
    return terms(text)


def hashed(text: str, dims: int = HASH_DIMS) -> list[float]:
    """A request or memory as hashed words, for a team with no embedder."""
    v = [0.0] * dims
    counts: dict[str, int] = {}
    for t in _terms(text):
        counts[t] = counts.get(t, 0) + 1
    for t, c in counts.items():
        h = int(hashlib.md5(t.encode()).hexdigest(), 16)
        v[h % dims] += (1.0 if (h >> 64) & 1 else -1.0) * (1 + math.log(c))
    return _unit(v)


def _unit(v):
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / s for x in v]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


_PROJ: dict[int, list[list[float]]] = {}


def project(v: list[float]) -> list[float]:
    """`v` in PROTO_DIMS dimensions, by a fixed random projection: cosines are
    kept closely enough, and a memory's evidence costs a few hundred numbers."""
    n = len(v)
    if n not in _PROJ:
        rng = random.Random(n)
        _PROJ[n] = [[rng.gauss(0, 1) for _ in range(n)] for _ in range(PROTO_DIMS)]
    return _unit([_dot(row, v) for row in _PROJ[n]])


def _sigmoid(z):
    return 1 / (1 + math.exp(-max(-60.0, min(60.0, z))))


class Fast:
    """One agent's fast scorer, kept in <team_dir>/recall/<agent>.fast.json.
    `space` names the vectors it works in; a different one starts afresh,
    since prototypes from one space mean nothing in another."""

    def __init__(self, path: Path, space: str):
        self.path, self.space = Path(path), space
        try:
            st = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        if st.get("space") != space:
            st = {}
        self.w = {**START, **(st.get("w") or {})}
        self.n = int(st.get("n") or 0)
        self.q = int(st.get("q") or 0)              # requests learned from
        self.mu = st.get("mu")                      # the average request, projected
        self.mem: dict[str, dict] = st.get("mem") or {}

    def save(self, keep=None) -> None:
        if keep is not None:
            self.mem = {k: v for k, v in self.mem.items() if k in keep}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"space": self.space, "w": self.w, "n": self.n, "q": self.q,
                                   "mu": self.mu, "mem": self.mem}), encoding="utf-8")
        os.replace(tmp, self.path)

    # --- scoring -------------------------------------------------------------

    def centred(self, qv) -> list[float]:
        p = project(qv)
        return _unit([x - m for x, m in zip(p, self.mu)]) if self.mu else p

    def features(self, qv, mid: str, mvec, words: float, qp=None) -> list[float]:
        e = self.mem.get(mid) or {}
        qp = qp if qp is not None else self.centred(qv)
        pos = max((_dot(qp, p["v"]) for p in e.get("pos") or []), default=0.0)
        neg = max((_dot(qp, p["v"]) for p in e.get("neg") or []), default=0.0)
        prior = (e.get("applied", 0) + 1) / (e.get("seen", 0) + 2) - 0.5
        sim = _dot(qv, mvec) if mvec else 0.0
        return [sim, words, pos, neg, prior]

    def score(self, f: list[float]) -> float:
        return _sigmoid(self.w["bias"] + sum(self.w[k] * x for k, x in zip(FEATURES, f)))

    def rank(self, qv, mvecs: dict, words: dict) -> list[dict]:
        """[{"id", "score", "f"}] for every memory, best first."""
        qp = self.centred(qv)
        out = []
        for mid, mv in mvecs.items():
            f = self.features(qv, mid, mv, words.get(mid, 0.0), qp)
            out.append({"id": mid, "score": self.score(f), "f": f})
        return sorted(out, key=lambda r: -r["score"])

    @staticmethod
    def picks(ranked: list[dict], most: int) -> list[str]:
        return [r["id"] for r in ranked if r["score"] >= THRESHOLD][:most]

    def explore(self, ranked: list[dict], have: set, k: int) -> list[str]:
        """Up to `k` memories, not already candidates, for the judge to grade:
        the best ranked, then by score plus a bonus that shrinks as the judge
        grades a memory (an upper confidence bound)."""
        rest = [r for r in ranked if r["id"] not in have]
        out = [r["id"] for r in rest[:1]][:k]
        bonus = sorted(rest[1:], key=lambda r: -(r["score"] + 1 / math.sqrt(
            1 + (self.mem.get(r["id"]) or {}).get("seen", 0))))
        return out + [r["id"] for r in bonus[: max(0, k - len(out))]]

    # --- learning ------------------------------------------------------------

    def learn(self, qv, labels, feats: dict, judged: bool = False) -> None:
        """Move the weights toward `labels` [(memory id, 0 or 1, weight)], and
        keep the request as evidence on each memory: where it applied, and
        where it did not although the fast path thought it would (a negative
        it would otherwise repeat). `judged`: these were the judge's
        candidates, so each counts toward its memory's rate of being chosen."""
        qp = self.centred(qv)
        for mid, y, weight in labels:
            f = feats.get(mid)
            if f is None:
                continue
            p = self.score(f)
            self.n += 1
            step = STEP / math.sqrt(1 + self.n / 50)
            g = (y - p) * weight
            for k, x in zip(("bias",) + FEATURES, [1.0] + f):
                w = self.w[k] + step * (g * x - PULL * (self.w[k] - START[k]))
                self.w[k] = max(-CLIP, min(CLIP, w))
            e = self.mem.setdefault(mid, {"seen": 0, "applied": 0, "pos": [], "neg": []})
            if judged:
                e["seen"] += 1
                e["applied"] += int(y)
            if y:
                _remember(e.setdefault("pos", []), qp)
            elif p >= THRESHOLD or not judged:
                _remember(e.setdefault("neg", []), qp)
        raw = project(qv)
        self.q += 1
        rate = max(MEAN_RATE, 1 / self.q)
        self.mu = [round((1 - rate) * m + rate * x, 5)
                   for m, x in zip(self.mu or [0.0] * len(raw), raw)]


def _remember(protos: list[dict], v: list[float]) -> None:
    """Add `v`; past PROTOS, merge the two closest, weighted by what each
    already summarises."""
    protos.append({"v": [round(x, 4) for x in v], "n": 1})
    if len(protos) <= PROTOS:
        return
    best, pair = -2.0, (0, 1)
    for i in range(len(protos)):
        for j in range(i + 1, len(protos)):
            d = _dot(protos[i]["v"], protos[j]["v"])
            if d > best:
                best, pair = d, (i, j)
    a, b = protos[pair[0]], protos[pair[1]]
    n = a["n"] + b["n"]
    merged = _unit([(x * a["n"] + y * b["n"]) / n for x, y in zip(a["v"], b["v"])])
    protos[pair[0]] = {"v": [round(x, 4) for x in merged], "n": n}
    del protos[pair[1]]

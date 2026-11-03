#!/usr/bin/env bash
# Build the image and check that it serves, keeps teams and restarts.
#
#   ENGINE=docker evals/container_smoke.sh     # what CI runs, on Docker Engine
#   ENGINE=podman evals/container_smoke.sh     # the same, rootless
#
# No key is passed in, so no agent runs: this checks the container, not a
# model. Exits non-zero on the first thing that is wrong.
set -euo pipefail

ENGINE=${ENGINE:-docker}
TAG=${TAG:-a-team-smoke}
PORT=${PORT:-18765}
NAME=a-team-smoke-$$
VOL=a-team-smoke-$$
cd "$(dirname "$0")/.."

cleanup() {
    "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true
    "$ENGINE" volume rm -f "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; "$ENGINE" logs "$NAME" >&2 || true; exit 1; }

# The URL from the latest start, which carries a new token each time.
token() {
    for _ in $(seq 60); do
        t=$("$ENGINE" logs "$NAME" 2>&1 | grep -o 'token=[A-Za-z0-9_-]*' | tail -1 | cut -d= -f2 || true)
        if [ -n "$t" ] && [ "$t" != "${1:-}" ]; then
            echo "$t"
            return
        fi
        sleep 1
    done
    fail "no URL printed within 60s"
}

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

"$ENGINE" build -q -t "$TAG" . >/dev/null
"$ENGINE" volume create "$VOL" >/dev/null
"$ENGINE" run -d --name "$NAME" -p "127.0.0.1:$PORT:8765" -v "$VOL:/data" "$TAG" >/dev/null

T=$(token)
[ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$T"
base="http://127.0.0.1:$PORT"
for _ in $(seq 30); do [ "$(code "$base/" )" != 000 ] && break; sleep 1; done

[ "$(code "$base/")" = 401 ] || fail "the page answered without a token"
[ "$(code "$base/?token=$T")" = 200 ] || fail "the page did not open with the printed token"
[ "$(code -H "Host: evil.example:$PORT" "$base/?token=$T")" = 403 ] || fail "a foreign Host header was answered"
setup=$(curl -s -H "X-Token: $T" "$base/api/setup")
echo "$setup" | grep -q '"runtimes"' || fail "/api/setup did not list runtimes: $setup"

uid=$("$ENGINE" exec "$NAME" id -u)
[ "$uid" = 1000 ] || fail "the app runs as uid $uid, not the ateam user"
"$ENGINE" exec "$NAME" sh -c 'touch /data/teams/.probe 2>/dev/null || mkdir -p /data/teams && touch /data/teams/.probe' \
    || fail "the ateam user cannot write the volume"
"$ENGINE" exec "$NAME" a-team --help >/dev/null || fail "a-team is not on PATH"

"$ENGINE" restart "$NAME" >/dev/null
T2=$(token "$T")
[ -n "${GITHUB_ACTIONS:-}" ] && echo "::add-mask::$T2"
for _ in $(seq 30); do [ "$(code "$base/?token=$T2")" = 200 ] && break; sleep 1; done
[ "$(code "$base/?token=$T2")" = 200 ] || fail "the app did not come back after a restart"
"$ENGINE" exec "$NAME" test -e /data/teams/.probe || fail "the volume did not survive a restart"

echo "container smoke: ok ($ENGINE)"

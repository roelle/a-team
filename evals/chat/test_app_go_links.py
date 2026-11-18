"""Go links with more than one prefix: "go/name", "x/name" and so on, each
to its own address, set in Preferences. Asked for: "just a list (dict) of
"x/" --> "http://x/"". The page's own renderer is run under node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "ateam/chat/static/common.js"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def run(stored, script: str):
    """Load common.js with `stored` as this browser's saved preferences,
    then print the JSON of `script`'s value."""
    prog = f"""
const vm = require("vm"), fs = require("fs");
const store = {{ "agychat.prefs": {json.dumps(json.dumps(stored)) if stored is not None else "null"} }};
const ctx = {{ localStorage: {{ getItem: (k) => store[k] ?? null, setItem: (k, v) => {{ store[k] = v; }} }},
              document: {{ addEventListener() {{}} }}, Intl, console }};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(COMMON))}, "utf8") + "\\n;globalThis.__out = (" + {json.dumps(script)} + ");", ctx);
process.stdout.write(JSON.stringify(ctx.__out));
"""
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_go_links_default_to_go_slash():
    html = run(None, 'inlineHTML("see go/runbook and x/thing")')
    assert 'href="http://go/runbook"' in html
    assert "x/thing" in html and "http://x/" not in html


def test_each_prefix_links_to_its_own_address():
    stored = {"golinks": {"go": "http://go/", "x": "http://x", "wiki": "https://wiki.example.com/w/"}}
    html = run(stored, 'inlineHTML("go/a, x/b-1 and wiki/Main_Page#top")')
    assert 'href="http://go/a"' in html
    assert 'href="http://x/b-1"' in html                     # a slash is added to an address without one
    assert 'href="https://wiki.example.com/w/Main_Page#top"' in html


def test_a_prefix_inside_a_path_is_not_a_link():
    stored = {"golinks": {"x": "http://x/"}}
    html = run(stored, 'inlineHTML("src/x/foo and http://host/x/bar")')
    assert "http://x/foo" not in html and "http://x/bar" not in html


def test_bookmarks_and_markdown_links_use_the_same_prefixes():
    stored = {"golinks": {"x": "http://x/"}}
    assert run(stored, 'href("x/runbook")') == "http://x/runbook"
    assert run(stored, 'href("go/runbook")') is None          # go/ is not set here
    assert 'href="http://x/doc"' in run(stored, 'inlineHTML("[the doc](x/doc)")')


def test_a_single_address_saved_by_an_earlier_version_still_works():
    html = run({"golinks": "https://go.example.com/"}, 'inlineHTML("go/a")')
    assert 'href="https://go.example.com/a"' in html


def test_addresses_that_are_not_http_are_ignored():
    stored = {"golinks": {"x": "javascript:alert(1)//", "bad prefix": "http://y/"}}
    assert run(stored, "goLinks()") == []
    assert "<a" not in run(stored, 'inlineHTML("x/a")')


def test_the_preferences_text_is_read_one_line_at_a_time():
    got = run(None, 'parseGoLinks("go/ http://go/\\n x -> http://x/\\n\\nwiki = https://w/\\nnot a link")')
    assert got == {"map": {"go": "http://go/", "x": "http://x/", "wiki": "https://w/"}, "bad": ["not a link"]}
    assert run(None, 'parseGoLinks("")') == {"map": {}, "bad": []}


def test_no_prefixes_means_no_go_links():
    assert "<a" not in run({"golinks": {}}, 'inlineHTML("go/a")')


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))

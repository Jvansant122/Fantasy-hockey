"""The data the page reads, and the page's own script.

index.html reads data/players.json directly, so a renamed or dropped field breaks the site without any error in Actions."""
import json
import re
import shutil
import subprocess

import pytest

from conftest import ROOT

import check_data


@pytest.fixture(scope="module")
def data():
    return json.loads((ROOT / "data" / "players.json").read_text())


def test_committed_data_passes_the_publish_check(data):
    assert check_data.problems(data) == []


def test_check_catches_broken_data(data):
    broken = json.loads(json.dumps(data))
    del broken["players"][0]["games_left"]
    broken["nights"] = broken["nights"][:6]
    errs = check_data.problems(broken)
    assert any("missing fields" in e for e in errs) and any("nights" in e for e in errs)
    assert check_data.problems({"players": []}) and check_data.problems({**data, "players": data["players"][:5]})


def test_page_only_reads_fields_the_data_has(data):
    """Every p.<field> in index.html exists on some player (cr_news is optional and only present with news)."""
    html = (ROOT / "index.html").read_text()
    used = set(re.findall(r"\bp\.([a-z_]+)", html))
    have = set().union(*(p.keys() for p in data["players"])) | {"cr_news"}
    assert not used - have, used - have


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_parses(tmp_path):
    html = (ROOT / "index.html").read_text()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts
    js = tmp_path / "page.mjs" if any("import " in s for s in scripts) else tmp_path / "page.js"
    js.write_text("\n;\n".join(scripts))
    out = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr

"""The data the page reads, and the page's own script.

index.html reads data/players.json directly, so a renamed or dropped field breaks the site without any error in Actions."""
import json
import re
import shutil
import subprocess

import pytest

from conftest import ROOT

PLAYER_FIELDS = {"id", "name", "team", "pos", "slots", "owner", "team_id", "ir", "injury", "owned", "ppg", "cur_ppg", "last_ppg",
                 "games", "games_left", "light", "light_left", "nights"}
RATED_FIELDS = {"cr", "cr_fpg", "cr_games", "cr_dress", "cr_matched", "cr_why", "cr_pct"}


@pytest.fixture(scope="module")
def data():
    return json.loads((ROOT / "data" / "players.json").read_text())


def test_top_level(data):
    assert {"updated", "nights", "rated", "teams", "players"} <= set(data)
    assert len(data["nights"]) == 7
    for n in data["nights"]:
        assert {"sp", "date", "games", "light", "past"} <= set(n)
    assert data["players"]


def test_player_fields(data):
    for p in data["players"]:
        missing = PLAYER_FIELDS - set(p)
        assert not missing, (p.get("name"), missing)
        if data["rated"]:
            assert not RATED_FIELDS - set(p), p.get("name")


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

import json
import subprocess
import time
from pathlib import Path

import pytest

from rg2watch.cli import main
from rg2watch.models import Finding
from rg2watch.store import Store
from test_browse import BASE, P, add, doc, store

SCRIPTS = sorted((Path(__file__).parent.parent / "scripts").glob("*.sh"))


def seeded_db(tmp_path):
    db = tmp_path / "e.sqlite"
    s = Store(db)
    mem = store()
    s.upsert_election(mem.get_election(P))
    d = doc(BASE + "/extfile/a.pdf")
    add(s, d)
    s.add_finding(P, Finding("V1", "HIGH", d, "crossed out"), d.url)
    s.set_review(d.url, "escalate")
    s.close()
    return db


def test_status_prints_counts_and_watcher_state(tmp_path, capsys):
    db = seeded_db(tmp_path)
    assert main(["status", "--db", str(db), "--parent", P]) == 0
    out = capsys.readouterr().out
    assert "election 662405: Title" in out
    assert "RG-2 seen 1" in out and "escalated 1" in out and "findings HIGH 1, MEDIUM 0, LOW 0" in out
    assert "spend $0.00 of $" in out and "last alert none" in out
    assert out.rstrip().endswith("watcher not running")
    (tmp_path / "e").mkdir()
    status = tmp_path / "e" / "status.json"  # the page folder of e.sqlite
    status.write_text(json.dumps({"watching": True, "parent": P, "ts": time.time(), "url": "http://127.0.0.1:8771"}))
    main(["status", "--db", str(db)])
    assert capsys.readouterr().out.rstrip().endswith("watcher running\npage http://127.0.0.1:8771")
    status.write_text(json.dumps({"watching": True, "parent": P, "ts": time.time() - 120}))
    main(["status", "--db", str(db)])
    assert capsys.readouterr().out.rstrip().endswith("watcher not running")


def test_status_unknown_election(tmp_path, capsys):
    assert main(["status", "--db", str(tmp_path / "x.sqlite"), "--parent", "1"]) == 1
    assert "not registered" in capsys.readouterr().err


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_syntax(script):
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


@pytest.mark.parametrize("script", [p for p in SCRIPTS if not p.name.startswith("_")], ids=lambda p: p.name)
def test_script_rejects_non_numeric_id(script):
    for args in (["abc"], []):
        r = subprocess.run([str(script), *args], capture_output=True, text=True)
        assert r.returncode == 2 and "usage:" in r.stderr

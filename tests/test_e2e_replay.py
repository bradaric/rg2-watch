import json
import sqlite3
import threading
from pathlib import Path

from rg2watch.cli import main
from rg2watch.replay import make_replay_server

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.rik.parlament.gov.rs"


def make_fixture_dir(tmp_path: Path) -> Path:
    root = tmp_path / "fx" / "662405"
    root.mkdir(parents=True)
    recs = json.loads((FIXTURES / "get_election_662405_rg2_p1.json").read_text(encoding="utf-8"))["records"][:4]
    for i, rec in enumerate(recs):
        rec["_municipality_id"] = "101" if i < 2 else "102"
    (root / "election.json").write_text(json.dumps({
        "parent": "662405", "round": "660578", "election_type": "3", "title": "Test",
        "base_url": BASE, "page_html": ""}), encoding="utf-8")
    (root / "listing.json").write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    (root / "municipalities.json").write_text(json.dumps({"101": "A", "102": "B"}))
    (root / "stations.json").write_text(json.dumps({"101": {"10101": "1"}, "102": {"10201": "1"}}))
    return root


def test_e2e_replay_watch_once_alerts_on_faulted_rg9(tmp_path, capsys):
    root = make_fixture_dir(tmp_path)
    server = make_replay_server(root, port=0, speed=1e9, faults=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        db = tmp_path / "w.sqlite"
        cfg = tmp_path / "rg2watch.toml"
        cfg.write_text("max_rps = 1000.0\n")
        common = ["--db", str(db), "--config", str(cfg)]
        assert main(["add", f"{base}/zapisnici/662405", *common]) == 0
        assert main(["watch", "--parent", "662405", "--once", "--base-url", base, *common]) == 0
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert "[HIGH] D1" in out
    assert "RG-9 published" in out
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 0
    assert con.execute(
        "SELECT COUNT(*) FROM findings WHERE rule='D1' AND severity='HIGH' AND alerted_at IS NOT NULL"
    ).fetchone()[0] >= 1


def test_watch_unknown_parent_exits_2(tmp_path):
    assert main(["watch", "--parent", "nope", "--once", "--db", str(tmp_path / "x.sqlite")]) == 2

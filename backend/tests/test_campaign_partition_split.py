import json

import pytest

from app.db import connect
from app.modules import campaign_partition_split as cps
from app.seed import init_db
from app.services import ingest as ingest_svc


SEED = {
    "visitors": [
        {"id": "V1", "label": "访客甲", "created_at": "2026-11-17T08:00:00Z"},
    ],
    "hits": [],
}


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "clickspan-test.db"
    fx_dir = tmp_path / "fixtures"
    fx_dir.mkdir()
    (fx_dir / "seed_hits.json").write_text(
        json.dumps(SEED, ensure_ascii=False), encoding="utf-8"
    )
    import app.db as db_mod
    import app.seed as seed_mod
    import app.services.ingest as ingest_mod

    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "FIXTURES_DIR", fx_dir)
    monkeypatch.setattr(seed_mod, "FIXTURES_DIR", fx_dir)
    monkeypatch.setattr(ingest_mod, "FIXTURES_DIR", fx_dir)
    init_db()
    return fx_dir


def _run(fx_dir, hits, batch_id="B-T1"):
    (fx_dir / "ingest_batch.json").write_text(
        json.dumps({"batch_id": batch_id, "hits": hits}, ensure_ascii=False),
        encoding="utf-8",
    )
    return ingest_svc.run_batch()


def _sessions(conn, visitor="V1"):
    rows = conn.execute(
        "SELECT * FROM sessions WHERE visitor_id=? ORDER BY started_at, id", (visitor,)
    ).fetchall()
    return [dict(r) for r in rows]


def _member_codes(conn, sid):
    return {
        cps.normalize_code(r[0])
        for r in conn.execute(
            "SELECT DISTINCT campaign_id FROM hits WHERE session_id=? AND channel='primary'",
            (sid,),
        ).fetchall()
    }


def test_two_campaign_codes_produce_two_bound_sessions(db_env):
    """验收点 4：同访客连续灌两套活动码 → 两段会话各绑一码。"""
    res = _run(db_env, [
        {"id": "H1", "visitor_id": "V1", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/a", "event_name": "view", "campaign_id": "C-A"},
        {"id": "H2", "visitor_id": "V1", "event_ts": "2026-11-17T09:01:00Z",
         "page_path": "/a", "event_name": "click", "campaign_id": "C-A"},
        # 间隙仅 1 分钟，远未到空闲超时，但活动码 A→B 必须强制切
        {"id": "H3", "visitor_id": "V1", "event_ts": "2026-11-17T09:02:00Z",
         "page_path": "/b", "event_name": "view", "campaign_id": "C-B"},
        {"id": "H4", "visitor_id": "V1", "event_ts": "2026-11-17T09:03:00Z",
         "page_path": "/b", "event_name": "click", "campaign_id": "C-B"},
    ])
    assert res["ok"] is True
    assert res["report"]["campaign_splits"] == 1

    conn = connect()
    try:
        sessions = _sessions(conn)
        assert len(sessions) == 2
        old, new = sessions
        assert old["status"] == "closed"          # 旧段必须已收口
        assert new["status"] == "open"            # 不得新段已起而旧段仍 open
        assert cps.bound_campaign_code(conn, old["id"]) == "C-A"
        assert cps.bound_campaign_code(conn, new["id"]) == "C-B"

        old_hits = [r["id"] for r in conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts", (old["id"],))]
        new_hits = [r["id"] for r in conn.execute(
            "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts", (new["id"],))]
        assert old_hits == ["H1", "H2"]
        assert new_hits == ["H3", "H4"]
        # 硬约束：禁止跨活动击点写进旧段成员
        assert _member_codes(conn, old["id"]) == {"C-A"}
        assert _member_codes(conn, new["id"]) == {"C-B"}

        edges = cps.list_edges(conn, visitor_id="V1")
        split_edges = [e for e in edges if e["reason"] == cps.EDGE_REASON_SPLIT]
        assert len(split_edges) == 1
        edge = split_edges[0]
        assert edge["old_session_id"] == old["id"]
        assert edge["new_session_id"] == new["id"]
        assert edge["prev_campaign_code"] == "C-A"
        assert edge["next_campaign_code"] == "C-B"
        assert edge["split_hit_id"] == "H3"

        audit = cps.audit_all(conn, visitor_id="V1")
        assert all(a["consistent"] for a in audit)
    finally:
        conn.close()


def test_null_to_coded_and_coded_to_null(db_env):
    """空变有、有变空同样强制切分。"""
    res = _run(db_env, [
        {"id": "N1", "visitor_id": "V1", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/a", "event_name": "view"},
        {"id": "N2", "visitor_id": "V1", "event_ts": "2026-11-17T09:01:00Z",
         "page_path": "/a", "event_name": "view", "campaign_id": "C-X"},
        {"id": "N3", "visitor_id": "V1", "event_ts": "2026-11-17T09:02:00Z",
         "page_path": "/a", "event_name": "view", "campaign_id": ""},
    ])
    assert res["ok"] is True
    assert res["report"]["campaign_splits"] == 2

    conn = connect()
    try:
        sessions = _sessions(conn)
        assert len(sessions) == 3
        assert [cps.bound_campaign_code(conn, s["id"]) for s in sessions] == [
            None, "C-X", None
        ]
        codes = [(e["prev_campaign_code"], e["next_campaign_code"])
                 for e in cps.list_edges(conn, visitor_id="V1")
                 if e["reason"] == cps.EDGE_REASON_SPLIT]
        assert codes == [(None, "C-X"), ("C-X", None)]
        assert all(a["consistent"] for a in cps.audit_all(conn, visitor_id="V1"))
    finally:
        conn.close()


def test_same_code_keeps_single_open_session(db_env):
    """活动码未变：不切，仍按空闲超时规则并段。"""
    res = _run(db_env, [
        {"id": "S1", "visitor_id": "V1", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/a", "event_name": "view", "campaign_id": "C-A"},
        {"id": "S2", "visitor_id": "V1", "event_ts": "2026-11-17T09:00:30Z",
         "page_path": "/a", "event_name": "click", "campaign_id": " C-A "},
    ])
    assert res["ok"] is True
    assert res["report"]["campaign_splits"] == 0

    conn = connect()
    try:
        sessions = _sessions(conn)
        assert len(sessions) == 1
        assert sessions[0]["status"] == "open"
        assert sessions[0]["hit_count"] == 2
        assert _member_codes(conn, sessions[0]["id"]) == {"C-A"}
    finally:
        conn.close()


def test_single_open_cap_after_split(db_env):
    """切分后同访客至多一个 open 段。"""
    _run(db_env, [
        {"id": "K1", "visitor_id": "V1", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/a", "event_name": "view", "campaign_id": "C-A"},
        {"id": "K2", "visitor_id": "V1", "event_ts": "2026-11-17T09:01:00Z",
         "page_path": "/b", "event_name": "view", "campaign_id": "C-B"},
        {"id": "K3", "visitor_id": "V1", "event_ts": "2026-11-17T09:02:00Z",
         "page_path": "/c", "event_name": "view", "campaign_id": "C-C"},
    ])
    conn = connect()
    try:
        open_n = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE visitor_id='V1' AND status='open'"
        ).fetchone()["c"]
        assert open_n == 1
        closed = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE visitor_id='V1' AND status='closed'"
        ).fetchone()["c"]
        assert closed == 2
    finally:
        conn.close()


def test_replay_does_not_duplicate_sessions_or_edges(db_env):
    """幂等重放：第二批同击点不再产生段/边。"""
    hits = [
        {"id": "R1", "visitor_id": "V1", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/a", "event_name": "view", "campaign_id": "C-A"},
        {"id": "R2", "visitor_id": "V1", "event_ts": "2026-11-17T09:01:00Z",
         "page_path": "/b", "event_name": "view", "campaign_id": "C-B"},
    ]
    first = _run(db_env, hits, batch_id="B-R")
    assert first["ok"] and first["report"]["written"] == 2
    second = _run(db_env, hits, batch_id="B-R")
    assert second["ok"] and second["report"]["written"] == 0

    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM campaign_partition_edges WHERE reason='campaign_change'"
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_audit_detects_drift_and_batch_rolls_back(db_env, monkeypatch):
    """失败闭：核证发现绑定码与成员 campaign_id 集合不符 → 整批回滚不报成功。"""
    ok = _run(db_env, [
        {"id": "D1", "visitor_id": "V1", "event_ts": "2026-11-17T09:00:00Z",
         "page_path": "/a", "event_name": "view", "campaign_id": "C-A"},
    ])
    assert ok["ok"]

    conn = connect()
    try:
        sess = conn.execute("SELECT id FROM sessions").fetchone()["id"]
        # 模拟漂移：成员击点被改写成别的活动码，与段绑定码对不上
        conn.execute("UPDATE hits SET campaign_id='C-Z' WHERE session_id=?", (sess,))
        conn.commit()
    finally:
        conn.close()

    res = _run(db_env, [
        {"id": "D2", "visitor_id": "V1", "event_ts": "2026-11-17T09:01:00Z",
         "page_path": "/b", "event_name": "view", "campaign_id": "C-B"},
    ])
    assert res["ok"] is False
    assert res["violations"]
    assert any(v["mismatch"] == "bound_code_mismatch" for v in res["violations"])

    # 整批回滚：D2 击点、新段、分区边均不得落库
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM hits WHERE id='D2'").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM campaign_partition_edges WHERE reason='campaign_change'"
        ).fetchone()["c"] == 0
        # 只读核证接口能检出漂移
        audit = cps.audit_session(conn, sess)
        assert audit["consistent"] is False
    finally:
        conn.close()


def test_seed_sessions_get_partition_edge_and_pass_audit(tmp_path, monkeypatch):
    """基座种子段也有分区边（绑定空码），核证一致。"""
    db_path = tmp_path / "clickspan-seed.db"
    fx_dir = tmp_path / "fixtures"
    fx_dir.mkdir()
    (fx_dir / "seed_hits.json").write_text(json.dumps({
        "visitors": [
            {"id": "V9", "label": "访客九", "created_at": "2026-11-17T08:00:00Z"},
        ],
        "hits": [
            {"id": "G1", "visitor_id": "V9", "event_ts": "2026-11-17T08:10:00Z",
             "page_path": "/home", "event_name": "view"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    import app.db as db_mod
    import app.seed as seed_mod
    import app.services.ingest as ingest_mod

    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "FIXTURES_DIR", fx_dir)
    monkeypatch.setattr(seed_mod, "FIXTURES_DIR", fx_dir)
    monkeypatch.setattr(ingest_mod, "FIXTURES_DIR", fx_dir)
    init_db()

    conn = connect()
    try:
        edge = conn.execute(
            "SELECT * FROM campaign_partition_edges WHERE reason='seed'"
        ).fetchone()
        assert edge is not None
        assert edge["next_campaign_code"] is None
        assert all(a["consistent"] for a in cps.audit_all(conn))
    finally:
        conn.close()

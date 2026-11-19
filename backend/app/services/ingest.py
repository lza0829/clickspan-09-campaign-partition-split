import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import campaign_partition_split as cps


def run_batch() -> dict:
    """收流：按夹具顺序追加击点，活动码变更时同事务收口旧段并起新段。

    失败闭（题目二选一）：**收流口同事务收口旧段 + 起新段**。
    每点处理顺序为「查 open 段与绑定活动码 → 变更则先 close 旧段、
    再 open 新段并写分区边 → 击点落新段成员」，全程一个事务；
    commit 前再跑只读核证，发现会话绑定码与成员 campaign_id 集合对不上
    则整体回滚并返回 ok=False。
    """
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    hits = payload.get("hits", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        written = 0
        splits = 0
        for hit in hits:
            vid = hit["visitor_id"]
            # 幂等重放：击点已收流过则整步跳过，不得再建段/写边
            if conn.execute(
                "SELECT 1 FROM hits WHERE id=? AND channel='primary'",
                (hit["id"],),
            ).fetchone():
                continue
            exists = conn.execute("SELECT 1 FROM visitors WHERE id=?", (vid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
                    (vid, hit.get("visitor_label") or vid, _now()),
                )
            code = cps.normalize_code(hit.get("campaign_id"))
            sess = conn.execute(
                """
                SELECT id FROM sessions
                WHERE visitor_id=? AND status='open'
                ORDER BY started_at DESC, id DESC LIMIT 1
                """,
                (vid,),
            ).fetchone()

            if sess is None:
                # 无 open 段：起新段承接，记 open_init 边（前后码都留痕）
                sid = _open_session(conn, vid, hit["event_ts"])
                cps.record_edge(
                    conn,
                    visitor_id=vid,
                    old_session_id=None,
                    new_session_id=sid,
                    prev_campaign_code=None,
                    next_campaign_code=code,
                    split_event_ts=hit["event_ts"],
                    split_hit_id=hit["id"],
                    reason=cps.EDGE_REASON_INIT,
                    created_at=_now(),
                )
            else:
                current_sid = sess["id"]
                bound_code = cps.bound_campaign_code(conn, current_sid)
                if cps.has_changed(bound_code, code):
                    # 空变有 / 有变空 / A 变 B：先收口旧段，再起新段，
                    # 分区边与成员写入在同一事务内完成。
                    _close_session(conn, current_sid, hit["event_ts"])
                    sid = _open_session(conn, vid, hit["event_ts"])
                    cps.record_edge(
                        conn,
                        visitor_id=vid,
                        old_session_id=current_sid,
                        new_session_id=sid,
                        prev_campaign_code=bound_code,
                        next_campaign_code=code,
                        split_event_ts=hit["event_ts"],
                        split_hit_id=hit["id"],
                        reason=cps.EDGE_REASON_SPLIT,
                        created_at=_now(),
                    )
                    splits += 1
                else:
                    # 活动码未变：不切，仍按空闲超时规则并段（超时切窗另题）
                    sid = current_sid

            conn.execute(
                """
                INSERT OR IGNORE INTO hits(
                    id, visitor_id, session_id, event_ts, page_path, event_name, channel, campaign_id
                ) VALUES (?,?,?,?,?,?, 'primary', ?)
                """,
                (
                    hit["id"],
                    vid,
                    sid,
                    hit["event_ts"],
                    hit["page_path"],
                    hit["event_name"],
                    code,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                written += 1
                conn.execute(
                    """
                    UPDATE sessions
                    SET hit_count = hit_count + 1,
                        ended_at = CASE
                          WHEN ended_at IS NULL OR ended_at < ? THEN ?
                          ELSE ended_at END
                    WHERE id=?
                    """,
                    (hit["event_ts"], hit["event_ts"], sid),
                )
                ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
                        (sid,),
                    ).fetchall()
                ]
                sess_row = conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (sid,)
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO session_proofs(
                      session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        sid,
                        vid,
                        json.dumps(ids, ensure_ascii=False),
                        sess_row["hit_count"],
                        sess_row["started_at"],
                        sess_row["ended_at"],
                        _now(),
                    ),
                )

        # 提交前核证：本批触及的访客，其会话绑定活动码必须与成员击点
        # campaign_id 集合一致；不一致即回滚，禁止报成功。
        touched_visitors = {h["visitor_id"] for h in hits}
        violations = []
        for vid in touched_visitors:
            for item in cps.audit_all(conn, visitor_id=vid):
                if not item["consistent"]:
                    violations.append(item)
        if violations:
            conn.rollback()
            return {
                "ok": False,
                "error": "活动分区核证失败，批次已回滚",
                "violations": violations,
            }

        report = {
            "batch_id": batch_id,
            "written": written,
            "input": len(hits),
            "campaign_splits": splits,
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _open_session(conn, visitor_id: str, event_ts: str) -> str:
    sid = f"S-{visitor_id}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        """
        INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
        VALUES (?,?,?,?,0,'open')
        """,
        (sid, visitor_id, event_ts, event_ts),
    )
    return sid


def _close_session(conn, session_id: str, event_ts: str) -> None:
    # 收口时间不早于段内最后一点
    row = conn.execute(
        "SELECT MAX(event_ts) AS last_ts FROM hits WHERE session_id=?",
        (session_id,),
    ).fetchone()
    ended_at = max(filter(None, [row["last_ts"], event_ts]))
    conn.execute(
        "UPDATE sessions SET status='closed', ended_at=? WHERE id=?",
        (ended_at, session_id),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

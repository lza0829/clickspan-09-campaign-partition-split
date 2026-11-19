"""活动分区切（campaign_partition_split）。

击点带 campaign_id，当新击点的活动码与当前 open 段绑定的活动码不一致时
（空变有、有变空、A 变 B 都算），强制收口当前段并另起新段承接该点，
即使空闲间隙尚未达到超时。活动码未变时不干预，并段仍交给空闲超时规则。

落库形态（题目二选一）：**独立活动分区边**。
每条 ``campaign_partition_edges`` 行记录旧段/新段、前后活动码与触发击点；
会话绑定的活动码 = 指向该会话的最新一条入边（new_session_id）的
next_campaign_code。本模块不向 sessions 行写 campaign_snapshot 列。

本模块只提供纯读核证与无副作用的判定/落库助手；事务边界由收流口持有，
保证「收口旧段 → 起新段 → 写边 → 成员落段」在同一事务内原子提交。
"""
from __future__ import annotations

STATUS = "implemented"

EDGE_REASON_INIT = "open_init"          # 无 open 段时起新段
EDGE_REASON_SPLIT = "campaign_change"   # 活动码变更强制切分
EDGE_REASON_SEED = "seed"               # 基座种子段


def normalize_code(value) -> str | None:
    """活动码归一：None/空串/纯空白一律视为「空」。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def has_changed(prev_code, next_code) -> bool:
    """空变有、有变空、A 变 B 都算变更；归一后相等才算未变。"""
    return normalize_code(prev_code) != normalize_code(next_code)


def bound_campaign_code(conn, session_id: str) -> str | None:
    """会话当前绑定的活动码；无入边（理论上不应出现）返回 None。"""
    row = conn.execute(
        """
        SELECT next_campaign_code
        FROM campaign_partition_edges
        WHERE new_session_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    return None if row is None else row["next_campaign_code"]


def record_edge(
    conn,
    *,
    visitor_id: str,
    old_session_id: str | None,
    new_session_id: str,
    prev_campaign_code,
    next_campaign_code,
    split_event_ts: str,
    split_hit_id: str | None,
    reason: str,
    created_at: str,
) -> int:
    """落一条活动分区边（调用方须已持有事务）。"""
    cur = conn.execute(
        """
        INSERT INTO campaign_partition_edges(
            visitor_id, old_session_id, new_session_id,
            prev_campaign_code, next_campaign_code,
            split_event_ts, split_hit_id, reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            visitor_id,
            old_session_id,
            new_session_id,
            normalize_code(prev_campaign_code),
            normalize_code(next_campaign_code),
            split_event_ts,
            split_hit_id,
            reason,
            created_at,
        ),
    )
    return int(cur.lastrowid)


def list_edges(conn, visitor_id: str | None = None, session_id: str | None = None):
    """只读：列活动分区边，可按访客/会话过滤。"""
    sql = "SELECT * FROM campaign_partition_edges WHERE 1=1"
    args: list = []
    if visitor_id:
        sql += " AND visitor_id=?"
        args.append(visitor_id)
    if session_id:
        sql += " AND (old_session_id=? OR new_session_id=?)"
        args.extend((session_id, session_id))
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def audit_session(conn, session_id: str) -> dict | None:
    """只读核证单会话：绑定活动码 vs 成员击点 campaign_id 集合。"""
    sess = conn.execute(
        "SELECT id, visitor_id, status, hit_count FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if sess is None:
        return None
    edge = conn.execute(
        """
        SELECT * FROM campaign_partition_edges
        WHERE new_session_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    member_codes = {
        normalize_code(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT campaign_id FROM hits
            WHERE session_id=? AND channel='primary'
            """,
            (session_id,),
        ).fetchall()
    }
    member_count = conn.execute(
        "SELECT COUNT(*) AS c FROM hits WHERE session_id=? AND channel='primary'",
        (session_id,),
    ).fetchone()["c"]
    if edge is None:
        consistent = False
        mismatch = "missing_partition_edge"
        bound = None
    else:
        bound = edge["next_campaign_code"]
        consistent = member_codes == {bound}
        mismatch = None if consistent else "bound_code_mismatch"
    return {
        "session_id": sess["id"],
        "visitor_id": sess["visitor_id"],
        "status": sess["status"],
        "bound_campaign_code": bound,
        "member_campaign_codes": sorted(c for c in member_codes if c is not None),
        "member_has_null_code": None in member_codes,
        "member_hit_count": member_count,
        "declared_hit_count": sess["hit_count"],
        "has_partition_edge": edge is not None,
        "consistent": consistent,
        "mismatch": mismatch,
    }


def audit_all(conn, visitor_id: str | None = None) -> list[dict]:
    """只读核证全部（或指定访客的）会话。"""
    if visitor_id:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id=? ORDER BY started_at, id",
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM sessions ORDER BY started_at, id"
        ).fetchall()
    return [audit_session(conn, r["id"]) for r in rows]

from app.modules import campaign_partition_split as cps


def list_sessions(conn, visitor_id: str | None = None):
    if visitor_id:
        rows = conn.execute(
            """
            SELECT id, visitor_id, started_at, ended_at, hit_count, status
            FROM sessions WHERE visitor_id=?
            ORDER BY started_at, id
            """,
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, visitor_id, started_at, ended_at, hit_count, status
            FROM sessions ORDER BY started_at, id
            """
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["campaign_code"] = cps.bound_campaign_code(conn, r["id"])
        out.append(item)
    return out


def session_detail(conn, session_id: str):
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        return None
    hits = conn.execute(
        """
        SELECT id, visitor_id, session_id, event_ts, page_path, event_name, channel, campaign_id
        FROM hits WHERE session_id=? AND channel='primary'
        ORDER BY event_ts, id
        """,
        (session_id,),
    ).fetchall()
    proof = conn.execute(
        "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    edges = cps.list_edges(conn, session_id=session_id)
    detail = {
        "session": dict(sess),
        "hits": [dict(h) for h in hits],
        "proof": dict(proof) if proof else None,
        "campaign_code": cps.bound_campaign_code(conn, session_id),
        "partition_edges": edges,
    }
    detail["session"]["campaign_code"] = detail["campaign_code"]
    return detail

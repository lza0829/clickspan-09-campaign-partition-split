import json

from app.db import FIXTURES_DIR, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitors (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS hits (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  session_id TEXT REFERENCES sessions(id),
  event_ts TEXT NOT NULL,
  page_path TEXT NOT NULL,
  event_name TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'primary',
  campaign_id TEXT,
  UNIQUE(visitor_id, event_ts, page_path, event_name)
);

CREATE TABLE IF NOT EXISTS campaign_partition_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  old_session_id TEXT REFERENCES sessions(id),
  new_session_id TEXT NOT NULL REFERENCES sessions(id),
  prev_campaign_code TEXT,
  next_campaign_code TEXT,
  split_event_ts TEXT NOT NULL,
  split_hit_id TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  visitor_id TEXT NOT NULL,
  hit_ids_json TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_at TEXT NOT NULL
);
"""


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            ("1800",),
        )
    n = conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """对早期建库文件补列/补表（CREATE TABLE IF NOT EXISTS 不改已存在的表）。"""
    hit_cols = {r["name"] for r in conn.execute("PRAGMA table_info(hits)").fetchall()}
    if "campaign_id" not in hit_cols:
        conn.execute("ALTER TABLE hits ADD COLUMN campaign_id TEXT")
    edge_tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "campaign_partition_edges" not in edge_tables:
        conn.execute(
            """
            CREATE TABLE campaign_partition_edges (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              visitor_id TEXT NOT NULL REFERENCES visitors(id),
              old_session_id TEXT REFERENCES sessions(id),
              new_session_id TEXT NOT NULL REFERENCES sessions(id),
              prev_campaign_code TEXT,
              next_campaign_code TEXT,
              split_event_ts TEXT NOT NULL,
              split_hit_id TEXT,
              reason TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        # 老库已有会话补一条 seed 边，绑定空码，待收流口按「空变有」切分
        for s in conn.execute("SELECT id, visitor_id, started_at FROM sessions").fetchall():
            conn.execute(
                """
                INSERT INTO campaign_partition_edges(
                    visitor_id, old_session_id, new_session_id,
                    prev_campaign_code, next_campaign_code,
                    split_event_ts, split_hit_id, reason, created_at
                ) VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                """,
                (s["visitor_id"], None, s["id"], None, None, s["started_at"], None, "seed"),
            )


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hits.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for v in data.get("visitors", []):
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
            (v["id"], v["label"], v["created_at"]),
        )
    # 基座：按访客整段落进一个 open 会话（超时切分留给功能题空桩）
    for visitor_id, group in _group_hits(data.get("hits", [])).items():
        group = sorted(group, key=lambda h: (h["event_ts"], h["id"]))
        sid = f"S-{visitor_id}-1"
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) VALUES (?,?,?,?,?,?)",
            (
                sid,
                visitor_id,
                group[0]["event_ts"],
                group[-1]["event_ts"],
                len(group),
                "open",
            ),
        )
        hit_ids = []
        for h in group:
            code = h.get("campaign_id")
            code = code.strip() if isinstance(code, str) and code.strip() else None
            conn.execute(
                """
                INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel, campaign_id)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    h["id"],
                    visitor_id,
                    sid,
                    h["event_ts"],
                    h["page_path"],
                    h["event_name"],
                    h.get("channel", "primary"),
                    code,
                ),
            )
            hit_ids.append(h["id"])
        # 种子段的活动分区边：基座夹具不带活动语义，绑定为「空」，
        # 收流口首次灌入带码击点时会走「空变有」强制切分。
        seed_codes = {
            (h.get("campaign_id").strip()
             if isinstance(h.get("campaign_id"), str) and h.get("campaign_id").strip()
             else None)
            for h in group
        }
        seed_code = next(iter(seed_codes)) if len(seed_codes) == 1 else None
        conn.execute(
            """
            INSERT INTO campaign_partition_edges(
                visitor_id, old_session_id, new_session_id,
                prev_campaign_code, next_campaign_code,
                split_event_ts, split_hit_id, reason, created_at
            ) VALUES (?,?,?,?,?,?,?,?,datetime('now'))
            """,
            (visitor_id, None, sid, None, seed_code, group[0]["event_ts"], None, "seed"),
        )
        conn.execute(
            """
            INSERT INTO session_proofs(session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at)
            VALUES (?,?,?,?,?,?,datetime('now'))
            """,
            (
                sid,
                visitor_id,
                json.dumps(hit_ids, ensure_ascii=False),
                len(hit_ids),
                group[0]["event_ts"],
                group[-1]["event_ts"],
            ),
        )


def _group_hits(hits):
    out = {}
    for h in hits:
        out.setdefault(h["visitor_id"], []).append(h)
    return out


def idle_timeout_sec(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    return int(row["value"]) if row else 1800

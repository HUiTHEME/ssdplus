# -*- coding: utf-8 -*-
"""SQLite 存储层。

身份约定（很重要）：
  每块硬盘用 `disk_key` 标识，形如 `sn:<序列号>`；只有完全拿不到序列号时才用
  `dv:<设备名>` 兜底。设备名会随换盘位、换主板、加装硬盘而变化，序列号不会。

表设计：
  disks          每块盘（按设备名）的静态信息与最近一次快照，带 disk_key
  samples        采样点（累计读写 + 速率 + 温度 + 健康度 + 已用寿命），按 disk_key 归档
  counter_state  /proc/diskstats 累计值到"跨重启连续计数"的换算状态，按 disk_key 记
  bench          测速结果，带 disk_key / serial
  events         事件流水（告警、变化记录）
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS disks (
    device          TEXT PRIMARY KEY,
    disk_key        TEXT,
    model           TEXT,
    serial          TEXT,
    firmware        TEXT,
    capacity        INTEGER,
    protocol        TEXT,
    is_ssd          INTEGER,
    first_seen      INTEGER,
    last_seen       INTEGER,
    health          INTEGER,
    temp            INTEGER,
    power_on_hours  INTEGER,
    power_cycles    INTEGER,
    unsafe_shutdowns INTEGER,
    media_errors    INTEGER,
    tbw             INTEGER,
    tbr             INTEGER,
    percent_used    INTEGER,
    smart_ok        INTEGER DEFAULT 0,
    smart_error     TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    disk_key    TEXT,
    serial      TEXT,
    device      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    acc_read    INTEGER NOT NULL,
    acc_write   INTEGER NOT NULL,
    read_rate   INTEGER,
    write_rate  INTEGER,
    temp        INTEGER,
    health      INTEGER,
    percent_used INTEGER
);

CREATE TABLE IF NOT EXISTS counter_state (
    disk_key    TEXT PRIMARY KEY,
    device      TEXT,
    last_read   INTEGER,
    last_write  INTEGER,
    acc_read    INTEGER DEFAULT 0,
    acc_write   INTEGER DEFAULT 0,
    last_ts     INTEGER
);

CREATE TABLE IF NOT EXISTS bench (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    device      TEXT NOT NULL,
    disk_key    TEXT,
    serial      TEXT,
    model       TEXT,
    read_mbps   REAL,
    write_mbps  REAL,
    size_mb     INTEGER,
    note        TEXT,
    read_rounds  TEXT,
    write_rounds TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    disk_key    TEXT,
    device      TEXT,
    level       TEXT,
    rule        TEXT,
    message     TEXT
);
"""

DISK_FIELDS = (
    "device", "model", "serial", "firmware", "capacity", "protocol", "is_ssd",
    "health", "temp", "power_on_hours", "power_cycles", "unsafe_shutdowns",
    "media_errors", "tbw", "tbr", "percent_used", "smart_ok", "smart_error",
)


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str) -> None:
    """建库建表 + 增量迁移。只加列/补数据，绝不 DROP 用户的历史。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(SCHEMA)
        if version < 3:
            _migrate_to_v3(conn)
        added = _sync_columns(conn)
        if added:
            print(f"数据库结构补齐：新增 {added} 个字段", flush=True)
        _ensure_indexes(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """索引在迁移之后建立，老库补完列才不会报 no such column。"""
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_disks_key ON disks (disk_key)",
        "CREATE INDEX IF NOT EXISTS idx_samples_key_ts ON samples (disk_key, ts)",
        "CREATE INDEX IF NOT EXISTS idx_samples_device_ts ON samples (device, ts)",
        "CREATE INDEX IF NOT EXISTS idx_bench_key_ts ON bench (disk_key, ts)",
        "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)",
        "CREATE INDEX IF NOT EXISTS idx_events_key_rule ON events (disk_key, rule, ts)",
    ):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass


_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.S)


def _sync_columns(conn: sqlite3.Connection) -> int:
    """按 SCHEMA 把老库里缺失的列补齐（只加列，不删不改数据）。

    老版本升级上来的库，CREATE TABLE IF NOT EXISTS 不会补列，缺列会让写入直接报错
    （例如实测老库的 bench 表没有 note / read_rounds，测速结果就永远存不进去）。
    """
    added = 0
    for table, body in _TABLE_RE.findall(SCHEMA):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for raw in body.split(","):
            parts = raw.strip().split()
            if not parts:
                continue
            name = parts[0]
            if name.upper() in ("PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"):
                continue
            if name in existing:
                continue
            decl = ""
            if len(parts) > 1 and parts[1].upper() not in (
                "PRIMARY", "NOT", "UNIQUE", "DEFAULT", "CHECK", "REFERENCES",
            ):
                decl = parts[1]
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}".strip())
            existing.add(name)
            added += 1
    return added


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3：引入 disk_key（序列号身份），补齐列并回填历史数据。"""
    _ensure_columns(conn, "disks", {"disk_key": "TEXT"})
    _ensure_columns(conn, "samples", {"disk_key": "TEXT", "serial": "TEXT", "percent_used": "INTEGER"})
    _ensure_columns(conn, "bench", {"disk_key": "TEXT", "serial": "TEXT"})
    _ensure_columns(conn, "events", {"disk_key": "TEXT", "rule": "TEXT"})

    # 先把 disks 的 disk_key 按序列号补上
    conn.execute(
        "UPDATE disks SET disk_key = CASE"
        "   WHEN serial IS NOT NULL AND TRIM(serial) NOT IN ('', 'N/A', 'UNKNOWN', '000000000000')"
        "     THEN 'sn:' || TRIM(serial)"
        "   ELSE 'dv:' || device END"
        " WHERE disk_key IS NULL OR disk_key = ''"
    )
    # counter_state 原来以设备名为主键，改成 disk_key
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(counter_state)")}
    if "disk_key" not in cols:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS counter_state_v3 ("
            " disk_key TEXT PRIMARY KEY, device TEXT, last_read INTEGER, last_write INTEGER,"
            " acc_read INTEGER DEFAULT 0, acc_write INTEGER DEFAULT 0, last_ts INTEGER)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO counter_state_v3 (disk_key, device, last_read, last_write,"
            " acc_read, acc_write, last_ts)"
            " SELECT COALESCE((SELECT d.disk_key FROM disks d WHERE d.device = c.device),"
            "                 'dv:' || c.device),"
            "        c.device, c.last_read, c.last_write, c.acc_read, c.acc_write, c.last_ts"
            " FROM counter_state c"
        )
        conn.execute("DROP TABLE counter_state")
        conn.execute("ALTER TABLE counter_state_v3 RENAME TO counter_state")

    # 再回填 samples / bench
    conn.execute(
        "UPDATE samples SET disk_key = COALESCE("
        "   (SELECT d.disk_key FROM disks d WHERE d.device = samples.device),"
        "   'dv:' || samples.device)"
        " WHERE disk_key IS NULL OR disk_key = ''"
    )
    conn.execute(
        "UPDATE samples SET serial = COALESCE("
        "   (SELECT d.serial FROM disks d WHERE d.device = samples.device), '')"
        " WHERE serial IS NULL"
    )
    conn.execute(
        "UPDATE bench SET serial = COALESCE("
        "   (SELECT d.serial FROM disks d WHERE d.device = bench.device), '')"
        " WHERE serial IS NULL"
    )
    conn.execute(
        "UPDATE bench SET disk_key = COALESCE("
        "   (SELECT d.disk_key FROM disks d WHERE d.device = bench.device),"
        "   'dv:' || bench.device)"
        " WHERE disk_key IS NULL OR disk_key = ''"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_key_ts ON samples (disk_key, ts)")
    conn.commit()


# ------------------------------------------------------------------ 磁盘信息

def upsert_disk(conn: sqlite3.Connection, info: dict, ts: int) -> None:
    row = {k: info.get(k) for k in DISK_FIELDS}
    row["disk_key"] = info.get("key")
    row["is_ssd"] = 1 if info.get("is_ssd") else 0
    row["smart_ok"] = 1 if info.get("smart_ok") else 0
    existing = conn.execute("SELECT first_seen FROM disks WHERE device = ?", (row["device"],)).fetchone()
    first_seen = existing["first_seen"] if existing else ts
    conn.execute(
        """INSERT INTO disks (device, disk_key, model, serial, firmware, capacity, protocol, is_ssd,
                              first_seen, last_seen, health, temp, power_on_hours, power_cycles,
                              unsafe_shutdowns, media_errors, tbw, tbr, percent_used,
                              smart_ok, smart_error)
           VALUES (:device, :disk_key, :model, :serial, :firmware, :capacity, :protocol, :is_ssd,
                   :first_seen, :last_seen, :health, :temp, :power_on_hours, :power_cycles,
                   :unsafe_shutdowns, :media_errors, :tbw, :tbr, :percent_used,
                   :smart_ok, :smart_error)
           ON CONFLICT(device) DO UPDATE SET
               disk_key=excluded.disk_key, model=excluded.model, serial=excluded.serial,
               firmware=excluded.firmware, capacity=excluded.capacity, protocol=excluded.protocol,
               is_ssd=excluded.is_ssd, last_seen=excluded.last_seen, health=excluded.health,
               temp=excluded.temp, power_on_hours=excluded.power_on_hours,
               power_cycles=excluded.power_cycles, unsafe_shutdowns=excluded.unsafe_shutdowns,
               media_errors=excluded.media_errors, tbw=excluded.tbw, tbr=excluded.tbr,
               percent_used=excluded.percent_used, smart_ok=excluded.smart_ok,
               smart_error=excluded.smart_error""",
        {**row, "first_seen": first_seen, "last_seen": ts},
    )
    conn.commit()


def disk_meta(conn: sqlite3.Connection, disk_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM disks WHERE disk_key = ? ORDER BY last_seen DESC LIMIT 1", (disk_key,)
    ).fetchone()
    return dict(row) if row else None


def all_disk_meta(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM disks ORDER BY last_seen DESC").fetchall()
    return [dict(row) for row in rows]


# ------------------------------------------------------------------ 采样

def update_counter(conn: sqlite3.Connection, disk_key: str, device: str,
                   cur_read: int, cur_write: int, ts: int) -> tuple[int, int, int | None, int | None]:
    """把 /proc/diskstats 的"自开机累计"换算成跨重启、跨设备名单调递增的累计值。"""
    row = conn.execute(
        "SELECT last_read, last_write, acc_read, acc_write, last_ts FROM counter_state WHERE disk_key = ?",
        (disk_key,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO counter_state (disk_key, device, last_read, last_write, acc_read, acc_write, last_ts)"
            " VALUES (?, ?, ?, ?, 0, 0, ?)",
            (disk_key, device, cur_read, cur_write, ts),
        )
        conn.commit()
        return 0, 0, None, None

    last_read, last_write = row["last_read"] or 0, row["last_write"] or 0
    acc_read, acc_write = row["acc_read"] or 0, row["acc_write"] or 0
    last_ts = row["last_ts"] or ts

    rebooted = cur_read < last_read or cur_write < last_write
    d_read = cur_read if rebooted else max(0, cur_read - last_read)
    d_write = cur_write if rebooted else max(0, cur_write - last_write)
    acc_read += d_read
    acc_write += d_write

    dt = ts - last_ts
    read_rate = int(d_read / dt) if dt > 0 and not rebooted else None
    write_rate = int(d_write / dt) if dt > 0 and not rebooted else None

    conn.execute(
        "UPDATE counter_state SET device=?, last_read=?, last_write=?, acc_read=?, acc_write=?, last_ts=?"
        " WHERE disk_key=?",
        (device, cur_read, cur_write, acc_read, acc_write, ts, disk_key),
    )
    conn.commit()
    return acc_read, acc_write, read_rate, write_rate


def add_sample(conn: sqlite3.Connection, info: dict, ts: int, acc_read: int, acc_write: int,
               read_rate: int | None, write_rate: int | None) -> None:
    conn.execute(
        "INSERT INTO samples (disk_key, serial, device, ts, acc_read, acc_write,"
        " read_rate, write_rate, temp, health, percent_used)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (info.get("key"), info.get("serial") or "", info.get("device") or "", ts,
         acc_read, acc_write, read_rate, write_rate,
         info.get("temp"), info.get("health"), info.get("percent_used")),
    )
    conn.commit()


def prune(conn: sqlite3.Connection, retention_days: int) -> None:
    edge = int(time.time()) - max(1, retention_days) * 86400
    conn.execute("DELETE FROM samples WHERE ts < ?", (edge,))
    conn.commit()


def samples_for(conn: sqlite3.Connection, disk_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, acc_read, acc_write, read_rate, write_rate, temp, health, percent_used"
        " FROM samples WHERE disk_key = ? ORDER BY ts ASC",
        (disk_key,),
    ).fetchall()
    return [dict(row) for row in rows]


def daily_history(conn: sqlite3.Connection, disk_keys: list[str], days: int | None = None) -> list[dict]:
    """按天聚合读写增量与平均温度（多盘时求和/平均）。"""
    if not disk_keys:
        return []
    marks = ",".join("?" * len(disk_keys))
    where = f"disk_key IN ({marks})"
    params: list = [*disk_keys]
    if days:
        where += " AND ts >= ?"
        params.append(int(time.time()) - days * 86400)
    rows = conn.execute(
        f"""SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d,
                   MIN(acc_read) AS r0, MAX(acc_read) AS r1,
                   MIN(acc_write) AS w0, MAX(acc_write) AS w1,
                   AVG(temp) AS temp
            FROM samples
            WHERE {where}
            GROUP BY d ORDER BY d ASC""",
        params,
    ).fetchall()
    out = []
    for row in rows:
        out.append({
            "date": row["d"],
            "read": max(0, (row["r1"] or 0) - (row["r0"] or 0)),
            "write": max(0, (row["w1"] or 0) - (row["w0"] or 0)),
            "temp": round(row["temp"], 1) if isinstance(row["temp"], (int, float)) else None,
        })
    return out


def day_series(conn: sqlite3.Connection, disk_key: str, date: str,
               limit: int = 288) -> dict:
    """某一天（本地时区）的采样点，用于热力图悬停时看日内曲线。

    返回 {"total": 原始条数, "points": [...]}。点数超过 limit 时按时间分桶，
    每桶取读写速率与温度的最大值 —— 抽样不能把峰值抹掉，那正是用户想看的。
    """
    try:
        start = int(time.mktime(time.strptime(date, "%Y-%m-%d")))
    except (ValueError, TypeError):
        return {"total": 0, "points": []}
    rows = conn.execute(
        """SELECT ts, read_rate, write_rate, temp
           FROM samples
           WHERE disk_key = ? AND ts >= ? AND ts < ?
           ORDER BY ts ASC""",
        (disk_key, start, start + 86400),
    ).fetchall()
    total = len(rows)
    if not total:
        return {"total": 0, "points": []}

    def pack(ts, read_v, write_v, temp_v):
        return {"ts": int(ts), "read": int(read_v or 0),
                "write": int(write_v or 0), "temp": temp_v}

    if total <= limit:
        points = [pack(r["ts"], r["read_rate"], r["write_rate"], r["temp"]) for r in rows]
    else:
        step = (total + limit - 1) // limit
        points = []
        for i in range(0, total, step):
            chunk = rows[i:i + step]
            # 时间取桶内写入最高的那条，峰值时刻才对得上（而不是桶的起点）
            busiest = max(chunk, key=lambda r: (r["write_rate"] or 0))
            points.append(pack(
                busiest["ts"],
                max((r["read_rate"] or 0) for r in chunk),
                max((r["write_rate"] or 0) for r in chunk),
                max((r["temp"] or 0) for r in chunk) or None,
            ))
    return {"total": total, "points": points}


def count_samples(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()
    return int(row["n"] or 0) if row else 0


def daily_writes(conn: sqlite3.Connection, disk_key: str, days: int = 30) -> list[dict]:
    """近 N 天每天写入字节数，用于概况页迷你趋势。"""
    return daily_history(conn, [disk_key], days)


def daily_writes_map(conn: sqlite3.Connection, disk_keys: list[str],
                     days: int = 30) -> dict[str, list[int]]:
    """一次查出多块盘近 N 天的每日写入量，返回 key -> [bytes...]，按天升序。"""
    if not disk_keys:
        return {}
    marks = ",".join("?" * len(disk_keys))
    since = int(time.time()) - days * 86400
    rows = conn.execute(
        f"""SELECT disk_key,
                   strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d,
                   MIN(acc_write) AS w0, MAX(acc_write) AS w1
            FROM samples
            WHERE disk_key IN ({marks}) AND ts >= ?
            GROUP BY disk_key, d ORDER BY d ASC""",
        [*disk_keys, since],
    ).fetchall()
    out: dict[str, list[int]] = {key: [] for key in disk_keys}
    for row in rows:
        out.setdefault(row["disk_key"], []).append(max(0, (row["w1"] or 0) - (row["w0"] or 0)))
    return out


def life_inputs(conn: sqlite3.Connection, disk_key: str) -> dict:
    """寿命预估需要的原始数据：已用寿命百分比的首末样本、写入速率。"""
    out: dict = {"first": None, "last": None, "write_per_day": None, "span_days": 0}

    rows = conn.execute(
        "SELECT ts, percent_used, health FROM samples"
        " WHERE disk_key = ? AND percent_used IS NOT NULL ORDER BY ts ASC",
        (disk_key,),
    ).fetchall()
    used = [r for r in rows if isinstance(r["percent_used"], int)]
    if len(used) >= 2:
        out["first"] = {"ts": used[0]["ts"], "pct": used[0]["percent_used"]}
        out["last"] = {"ts": used[-1]["ts"], "pct": used[-1]["percent_used"]}

    # 健康度作为回退依据（SATA 盘的寿命属性 / NVMe 的 100-used）
    if out["first"] is None:
        rows = conn.execute(
            "SELECT ts, health FROM samples WHERE disk_key = ? AND health IS NOT NULL ORDER BY ts ASC",
            (disk_key,),
        ).fetchall()
        if len(rows) >= 2:
            out["first"] = {"ts": rows[0]["ts"], "pct": max(0, 100 - (rows[0]["health"] or 0))}
            out["last"] = {"ts": rows[-1]["ts"], "pct": max(0, 100 - (rows[-1]["health"] or 0))}

    if out["first"] and out["last"]:
        out["span_days"] = round((out["last"]["ts"] - out["first"]["ts"]) / 86400, 1)

    edge = int(time.time()) - 30 * 86400
    row = conn.execute(
        "SELECT MIN(acc_write) AS w0, MAX(acc_write) AS w1 FROM samples"
        " WHERE disk_key = ? AND ts >= ?",
        (disk_key, edge),
    ).fetchone()
    if row and row["w0"] is not None and row["w1"] is not None:
        out["write_per_day"] = max(0, (row["w1"] or 0) - (row["w0"] or 0)) / 30
    return out


# ------------------------------------------------------------------ 测速

def add_bench(conn: sqlite3.Connection, device: str, model: str, read_mbps: float,
              write_mbps: float | None, size_mb: int, note: str = "",
              read_rounds: list | None = None, write_rounds: list | None = None,
              disk_key: str | None = None, serial: str | None = None) -> None:
    conn.execute(
        "INSERT INTO bench (ts, device, disk_key, serial, model, read_mbps, write_mbps, size_mb,"
        " note, read_rounds, write_rounds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), device, disk_key, serial, model, read_mbps, write_mbps, size_mb,
         note, json.dumps(read_rounds or []), json.dumps(write_rounds or [])),
    )
    conn.commit()


def list_bench(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT * FROM bench ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for key in ("read_rounds", "write_rounds"):
            raw = item.get(key)
            try:
                item[key] = json.loads(raw) if raw else []
            except (TypeError, ValueError):
                item[key] = []
        out.append(item)
    return out


def clear_bench(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM bench")
    conn.commit()


def clear_events(conn: sqlite3.Connection) -> int:
    """清空事件流水（用户手动清理）。返回删掉的条数。"""
    cur = conn.execute("DELETE FROM events")
    conn.commit()
    return cur.rowcount or 0


# ------------------------------------------------------------------ 事件

def add_event(conn: sqlite3.Connection, disk_key: str | None, device: str | None,
              level: str, rule: str, message: str) -> None:
    conn.execute(
        "INSERT INTO events (ts, disk_key, device, level, rule, message) VALUES (?, ?, ?, ?, ?, ?)",
        (int(time.time()), disk_key, device, level, rule, message),
    )
    conn.commit()


def list_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def last_event_ts(conn: sqlite3.Connection, disk_key: str, rule: str) -> int | None:
    row = conn.execute(
        "SELECT ts FROM events WHERE disk_key = ? AND rule = ? ORDER BY ts DESC LIMIT 1",
        (disk_key, rule),
    ).fetchone()
    return int(row["ts"]) if row else None


# ------------------------------------------------------------------ 导入

def merge_samples(conn: sqlite3.Connection, disk_key: str, serial: str, device: str,
                  rows: list[dict]) -> tuple[int, int]:
    """按 (disk_key, ts) 去重合并。返回 (新增, 跳过)。"""
    existing = {
        r["ts"] for r in conn.execute(
            "SELECT ts FROM samples WHERE disk_key = ?", (disk_key,)
        ).fetchall()
    }
    added = skipped = 0
    for row in rows:
        ts = row.get("ts")
        if not isinstance(ts, int):
            continue
        if ts in existing:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO samples (disk_key, serial, device, ts, acc_read, acc_write,"
            " read_rate, write_rate, temp, health, percent_used)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (disk_key, serial, device, ts,
             int(row.get("read_bytes_total") or 0), int(row.get("write_bytes_total") or 0),
             row.get("read_rate_bps"), row.get("write_rate_bps"),
             row.get("temp_c"), row.get("health_pct"), row.get("percent_used")),
        )
        existing.add(ts)
        added += 1
    conn.commit()
    return added, skipped

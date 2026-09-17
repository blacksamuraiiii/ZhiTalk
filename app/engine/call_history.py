"""
Call History + 通话指标 SQLite 持久化模块
设计文档：docs/plan/plan-20260816-SQLite持久化设计.md

两张表：
  call_records — 通话级（每通 1 行），含聚合指标 + transcript
  call_logs   — 日志级（每通 N 行），与 call_records 通过 call_id 关联
"""

import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ai-backend.call_history")

# ── 时区（Asia/Shanghai，tzdata 缺失兜底 +08:00）──
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    from datetime import timedelta, timezone
    _TZ = timezone(timedelta(hours=8))

# ── 数据库路径 ──
DB_PATH = os.environ.get("CALL_HISTORY_DB_PATH", "/app/data/metrics/calls.db")

# ── 连接单例 ──
_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("PRAGMA busy_timeout=30000;")
        _conn.execute("PRAGMA foreign_keys=ON;")
        _init_tables(_conn)
        _migrate(_conn)
    return _conn


# ── 建表 ──

def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS call_records (
            id              TEXT PRIMARY KEY,
            call_id         TEXT UNIQUE,
            parent_call_id  TEXT,
            caller          TEXT,
            callee          TEXT,
            is_callback     INTEGER DEFAULT 0,
            start_ts        REAL NOT NULL,
            end_ts          REAL,
            duration_s      REAL,
            day             TEXT,
            asr             TEXT,
            tts             TEXT,
            llm             TEXT,
            vad             TEXT,
            outcome         TEXT NOT NULL DEFAULT 'hangup',
            error           TEXT,
            has_search      INTEGER DEFAULT 0,
            search_domains  TEXT,
            turn_count      INTEGER DEFAULT 0,
            turn_min_ms     REAL,
            turn_avg_ms     REAL,
            turn_max_ms     REAL,
            turn_p95_ms     REAL,
            ttft_avg_ms     REAL,
            asr_avg_ms      REAL,
            llm_avg_ms      REAL,
            tts_avg_ms      REAL,
            transcript      TEXT,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_call_records_day
            ON call_records(day);
        CREATE INDEX IF NOT EXISTS idx_call_records_caller
            ON call_records(caller);

        CREATE TABLE IF NOT EXISTS call_logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id   TEXT,
            ts        REAL NOT NULL,
            level     TEXT,
            msg       TEXT NOT NULL,
            turn      INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_call_logs_call_id
            ON call_logs(call_id);
        CREATE INDEX IF NOT EXISTS idx_call_logs_ts
            ON call_logs(ts);

        CREATE TABLE IF NOT EXISTS scheduler_runs (
            id            TEXT PRIMARY KEY,
            job_name      TEXT,
            trigger_type  TEXT,
            caller        TEXT,
            callee        TEXT,
            content       TEXT,
            reply         TEXT,
            status        TEXT NOT NULL DEFAULT 'executing',
            error         TEXT,
            run_ts        REAL NOT NULL,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_scheduler_runs_ts
            ON scheduler_runs(run_ts);
    """)
    conn.commit()
    logger.info(f"数据库初始化完成: {DB_PATH}")


def _migrate(conn: sqlite3.Connection):
    """增量迁移：只加列，不删列不改名（schema 一次定稿，长期只增）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(call_records)")}
    if "callee" not in cols:
        conn.execute("ALTER TABLE call_records ADD COLUMN callee TEXT")
        conn.commit()
        logger.info("迁移: call_records 新增 callee 列")


# ── 辅助函数 ──

def _p95(values: list[float]) -> Optional[float]:
    """最近秩法：第 ceil(0.95*n) 个有序值（1-based 转 0-based）。空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    idx = max(0, math.ceil(0.95 * len(s)) - 1)
    return s[idx]


def _avg(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _min_avg_max_p95(values: list[float]) -> tuple:
    """返回 (min, avg, max, p95)；空列表返回全 None。"""
    if not values:
        return (None, None, None, None)
    s = sorted(values)
    return (s[0], sum(s) / len(s), s[-1], _p95(s))


# ── 写入 call_records ──

def insert_record(
    call_id: str = "",
    parent_call_id: Optional[str] = None,
    caller: str = "",
    callee: str = "",
    is_callback: int = 0,
    start_ts: float = 0.0,
    end_ts: float = 0.0,
    asr: str = "",
    tts: str = "",
    llm: str = "",
    vad: str = "",
    outcome: str = "hangup",
    error: Optional[str] = None,
    has_search: int = 0,
    search_domains: str = "",
    turn_metrics: Optional[list[dict]] = None,
):
    """
    通话结束时调用（finally 块内），写入一条 call_records。
    turn_metrics 为空列表时，时延列均存 NULL。
    """
    import uuid

    duration_s = round(end_ts - start_ts, 1) if end_ts > start_ts else 0.0
    day = datetime.fromtimestamp(start_ts, tz=_TZ).strftime("%Y-%m-%d") if start_ts else ""
    turn_metrics = turn_metrics or []

    # ── 守卫：跳过垃圾记录（AudioSocket 连接失败时产生的空记录）──
    turn_count = len(turn_metrics)
    if turn_count == 0 and outcome == "hangup" and not call_id:
        return

    # ── 聚合时延 ──
    turn_times = [t["turn_ms"] for t in turn_metrics if t.get("turn_ms")]
    ttft_times = [t["ttft_ms"] for t in turn_metrics if t.get("ttft_ms")]
    asr_times  = [t["asr_ms"]  for t in turn_metrics if t.get("asr_ms")]
    llm_times  = [t["llm_total_ms"] for t in turn_metrics if t.get("llm_total_ms")]
    tts_times  = [t["tts_ms"]  for t in turn_metrics if t.get("tts_ms")]

    turn_min, turn_avg, turn_max, turn_p95 = _min_avg_max_p95(turn_times)
    ttft_avg = _avg(ttft_times)
    asr_avg  = _avg(asr_times)
    llm_avg  = _avg(llm_times)
    tts_avg  = _avg(tts_times)

    transcript_json = json.dumps(turn_metrics, ensure_ascii=False) if turn_metrics else None

    record = {
        "id": str(uuid.uuid4()),
        "call_id": call_id,
        "parent_call_id": parent_call_id,
        "caller": caller,
        "callee": callee,
        "is_callback": is_callback,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_s": duration_s,
        "day": day,
        "asr": asr,
        "tts": tts,
        "llm": llm,
        "vad": vad,
        "outcome": outcome,
        "error": error,
        "has_search": has_search,
        "search_domains": search_domains,
        "turn_count": turn_count,
        "turn_min_ms": turn_min,
        "turn_avg_ms": turn_avg,
        "turn_max_ms": turn_max,
        "turn_p95_ms": turn_p95,
        "ttft_avg_ms": ttft_avg,
        "asr_avg_ms": asr_avg,
        "llm_avg_ms": llm_avg,
        "tts_avg_ms": tts_avg,
        "transcript": transcript_json,
    }

    def _insert():
        conn = _get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO call_records (
                    id, call_id, parent_call_id, caller, callee, is_callback,
                    start_ts, end_ts, duration_s, day,
                    asr, tts, llm, vad,
                    outcome, error,
                    has_search, search_domains,
                    turn_count, turn_min_ms, turn_avg_ms, turn_max_ms, turn_p95_ms,
                    ttft_avg_ms, asr_avg_ms, llm_avg_ms, tts_avg_ms,
                    transcript
                ) VALUES (
                    :id, :call_id, :parent_call_id, :caller, :callee, :is_callback,
                    :start_ts, :end_ts, :duration_s, :day,
                    :asr, :tts, :llm, :vad,
                    :outcome, :error,
                    :has_search, :search_domains,
                    :turn_count, :turn_min_ms, :turn_avg_ms, :turn_max_ms, :turn_p95_ms,
                    :ttft_avg_ms, :asr_avg_ms, :llm_avg_ms, :tts_avg_ms,
                    :transcript
                )
            """, record)
            conn.commit()
            logger.info(f"[CALLHISTORY] 写入通话记录: call_id={call_id} duration={duration_s}s turns={len(turn_metrics)}")
        except Exception as e:
            logger.warning(f"[CALLHISTORY] 写入失败: {e}")
    try:
        _insert()
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 调用失败: {e}")


# ── 写入 call_logs ──

def insert_log(call_id: Optional[str], level: str, msg: str, turn: Optional[int] = None):
    """_log() 双写：内存 queue + 此方法。call_id 可能为 None（UUID 帧之前），存 NULL。"""
    def _insert():
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO call_logs (call_id, ts, level, msg, turn) VALUES (?, ?, ?, ?, ?)",
                (call_id or None, time.time(), level, msg, turn),
            )
            conn.commit()
        except Exception:
            pass  # 日志写入失败不抛异常、不记日志（防递归）
    try:
        _insert()
    except Exception:
        pass


# ── 读取（Metrics API 用）──

def load_all_calls() -> list[dict]:
    """返回全量摘要记录（无 LIMIT，供统计表聚合用）。"""
    try:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT caller, callee, is_callback, start_ts, day, duration_s, turn_count, outcome,
                      asr, tts, llm, ttft_avg_ms, turn_avg_ms, turn_p95_ms
               FROM call_records ORDER BY start_ts DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 读取全量失败: {e}")
        return []


def load_calls(limit: int = 10) -> list[dict]:
    """返回最近 N 条摘要记录（不含 transcript）。默认 10 条，供近期通话表格用。"""
    try:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT caller, callee, is_callback, start_ts, day, duration_s, turn_count, outcome,
                      asr, tts, llm, ttft_avg_ms, turn_avg_ms, turn_p95_ms
               FROM call_records ORDER BY start_ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 读取列表失败: {e}")
        return []


def get_summary(calls: list[dict]) -> dict:
    """从通话列表计算聚合统计（兼容旧 API 返回结构，新增 ttft/turn 时延聚合）。"""
    total = len(calls)
    if total == 0:
        return {
            "total": 0, "avg_duration_s": 0, "max_duration_s": 0, "min_duration_s": 0,
            "avg_turns": 0, "max_turns": 0,
            "timeout_count": 0, "callback_count": 0, "farewell_count": 0,
            "timeout_rate": 0, "avg_ttft_ms": 0, "avg_turn_ms": 0, "avg_p95_ms": 0,
        }
    durations = [c.get("duration_s", 0) or 0 for c in calls]
    turns = [c.get("turn_count", 0) or 0 for c in calls]
    timeout_count = sum(1 for c in calls if c.get("outcome") in ("timed_out", "idle_timeout", "greeting_timeout"))
    farewell_count = sum(1 for c in calls if c.get("outcome") == "farewell")
    callback_count = sum(1 for c in calls if c.get("is_callback"))
    # 时延聚合：只统计有对话的通话（turn_count>0 且有值）
    ttfts = [c["ttft_avg_ms"] for c in calls if c.get("turn_count") and c.get("ttft_avg_ms")]
    turns_ms = [c["turn_avg_ms"] for c in calls if c.get("turn_count") and c.get("turn_avg_ms")]
    p95s = [c["turn_p95_ms"] for c in calls if c.get("turn_count") and c.get("turn_p95_ms")]
    return {
        "total": total,
        "avg_duration_s": round(sum(durations) / len(durations), 1) if durations else 0,
        "max_duration_s": max(durations) if durations else 0,
        "min_duration_s": min(durations) if durations else 0,
        "avg_turns": round(sum(turns) / len(turns), 1) if turns else 0,
        "max_turns": max(turns) if turns else 0,
        "timeout_count": timeout_count,
        "callback_count": callback_count,
        "farewell_count": farewell_count,
        "timeout_rate": round(timeout_count / total * 100, 1) if total else 0,
        "avg_ttft_ms": round(sum(ttfts) / len(ttfts), 1) if ttfts else 0,
        "avg_turn_ms": round(sum(turns_ms) / len(turns_ms), 1) if turns_ms else 0,
        "avg_p95_ms": round(sum(p95s) / len(p95s), 1) if p95s else 0,
    }


def load_logs(call_id: str, limit: int = 200) -> list[dict]:
    """按 call_id 查日志（/api/logs 用）。"""
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT ts, level, msg, turn FROM call_logs WHERE call_id = ? ORDER BY ts LIMIT ?",
            (call_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 读取日志失败: {e}")
        return []


# ── 时间格式化 ──

def _fmt_ts(ts: float) -> str:
    """UNIX 时间戳 → 北京时间字符串（YYYY-MM-DD HH:MM:SS）。"""
    if not ts:
        return ""
    from datetime import datetime
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ── 历史查询（通话日志页用）──

def load_calls_paged(page: int = 1, page_size: int = 20, keyword: str = "") -> list[dict]:
    """分页查询通话记录，支持按 caller/callee/call_id 模糊搜索。"""
    try:
        conn = _get_conn()
        offset = (page - 1) * page_size
        where = ""
        params: list = []
        if keyword:
            where = "WHERE caller LIKE ? OR callee LIKE ? OR call_id LIKE ?"
            like = f"%{keyword}%"
            params = [like, like, like]
        rows = conn.execute(
            f"""SELECT call_id, caller, callee, start_ts, end_ts, duration_s,
                       is_callback, outcome, turn_count
                FROM call_records {where}
                ORDER BY start_ts DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["start_time"] = _fmt_ts(d.get("start_ts"))
            d["end_time"] = _fmt_ts(d.get("end_ts"))
            results.append(d)
        return results
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 分页查询失败: {e}")
        return []


def count_calls(keyword: str = "") -> int:
    """通话总数（分页用）。"""
    try:
        conn = _get_conn()
        where = ""
        params = []
        if keyword:
            where = "WHERE caller LIKE ? OR callee LIKE ? OR call_id LIKE ?"
            like = f"%{keyword}%"
            params = [like, like, like]
        row = conn.execute(
            f"SELECT COUNT(*) FROM call_records {where}",
            params,
        ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 计数失败: {e}")
        return 0


def load_call_detail(call_id: str) -> dict | None:
    """加载单条通话详情（含 transcript 解析 + 日志）。"""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM call_records WHERE call_id = ?",
            (call_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["start_time"] = _fmt_ts(d.get("start_ts"))
        d["end_time"] = _fmt_ts(d.get("end_ts"))
        # 解析 transcript JSON
        turns = []
        if d.get("transcript"):
            try:
                turns = json.loads(d["transcript"])
            except Exception:
                pass
        d["turns"] = turns
        # 关联日志
        d["logs"] = load_logs(call_id)
        return d
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 详情查询失败: {e}")
        return None


# ── 定时任务执行记录（scheduler_runs）──

def insert_scheduler_run(run_id: str, job_name: str, trigger_type: str,
                         caller: str, callee: str, content: str,
                         status: str = "executing", reply: str = "") -> None:
    """插入一条任务执行记录（status=executing）。"""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO scheduler_runs "
            "(id, job_name, trigger_type, caller, callee, content, reply, status, run_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, job_name, trigger_type, caller, callee, content, reply, status, time.time()),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 插入任务记录失败: {e}")


def update_scheduler_run(run_id: str, status: str, reply: str = "", error: str = "") -> None:
    """更新任务执行状态（success/failed）"""
    try:
        conn = _get_conn()
        conn.execute(
            "UPDATE scheduler_runs SET status = ?, reply = ?, error = ? WHERE id = ?",
            (status, reply, error, run_id),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 更新任务记录失败: {e}")


def load_scheduler_runs(limit: int = 10) -> list[dict]:
    """返回最近 N 条任务执行记录（含 trigger_type 转中文）。默认 10 条。"""
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, job_name, trigger_type, caller, callee, content, reply, "
            "status, error, run_ts FROM scheduler_runs ORDER BY run_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["trigger_label"] = "手动" if d.get("trigger_type") == "manual" else "自动"
            d["run_time"] = _fmt_ts(d.get("run_ts"))
            # 关联回拨通话 call_id（caller/callee 一致 + run_ts 后 120s 内的回拨记录）
            d["call_id"] = None
            try:
                _row = conn.execute(
                    "SELECT call_id FROM call_records WHERE is_callback=1 AND caller=? AND callee=? "
                    "AND start_ts >= ? AND start_ts <= ? ORDER BY start_ts ASC LIMIT 1",
                    (d.get("caller"), d.get("callee"), d.get("run_ts"), (d.get("run_ts") or 0) + 120),
                ).fetchone()
                if _row:
                    d["call_id"] = _row["call_id"]
            except Exception:
                pass
            results.append(d)
        return results
    except Exception as e:
        logger.warning(f"[CALLHISTORY] 读取任务记录失败: {e}")
        return []
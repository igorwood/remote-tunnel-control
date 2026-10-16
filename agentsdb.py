#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentsdb.py —— 代理会话与文件台账（SQLite 单库）

库文件：/opt/remote-tunnel-control/db/agents.db

存三类东西：
  1) agents        —— 每个代理（机器）的登记信息（与 machines.json 同步）
  2) sessions/messages —— 与每个代理的**每一次会话**及其往来消息
       session.kind: reasonix_task（交给远端 agent 的任务）｜ file_transfer ｜ control（控制面动作）｜ manual
       message.role : user（我们发给它）｜ agent（它回传）｜ system ｜ tool
  3) files         —— 他们**上传/传输的文件**（含大小、sha256、方向、状态）
  另有 events（注册/注销/运维审计）与 enroll_tickets（凭证台账）

用法（CLI）：
    python3 agentsdb.py init                       # 建库建表
    python3 agentsdb.py sync-registry              # 从 machines.json 同步代理
    python3 agentsdb.py import-audit               # 导入 enroll/{audit.jsonl,tickets.json}
    python3 agentsdb.py scan-uploads <目录> --machine machine-a   # 索引已有上传文件
    python3 agentsdb.py log-session --machine X --kind reasonix_task --title "..." [--status ok] [--duration 3.2]
    python3 agentsdb.py log-message --session 1 --role user --content "..."
    python3 agentsdb.py log-file --machine X --direction upload --cloud-path /p --local-path C:\\p [--session 1]
    python3 agentsdb.py sessions [--machine X] [-n 20]
    python3 agentsdb.py files [--machine X] [-n 20]
    python3 agentsdb.py stats

作为库使用（供 rmt.py / push_remote_file.py / enroll_service.py 挂钩）：
    import agentsdb as adb
    sid = adb.log_session("machine2", "reasonix_task", title="统计产物", status="ok", duration_s=3.1)
    adb.log_message(sid, "user", task_text)
    adb.log_message(sid, "agent", output_text)
    adb.log_file("machine2", "upload", cloud_path=..., size_bytes=..., sha256=...)
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

BASE = "/opt/remote-tunnel-control"
DB_DIR = os.path.join(BASE, "db")
DB = os.path.join(DB_DIR, "agents.db")
REGISTRY = os.path.join(BASE, "machines.json")
ENROLL = os.path.join(BASE, "enroll")
MAX_MSG = 200000        # 消息正文入库上限（超出截断，避免库膨胀）

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS agents (
  machine_id TEXT PRIMARY KEY, hostname TEXT, desc TEXT, platform TEXT,
  control_port INTEGER, serve_port INTEGER, guard_base INTEGER,
  token_key TEXT, env_path TEXT, method TEXT,
  registered_at TEXT, last_seen TEXT, status TEXT DEFAULT 'active', meta TEXT);
CREATE TABLE IF NOT EXISTS sessions (
  session_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_id TEXT, kind TEXT, title TEXT,
  started_at TEXT, ended_at TEXT, status TEXT, cwd TEXT,
  duration_s REAL, summary TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS messages (
  msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER, ts TEXT, role TEXT,
  content TEXT, content_len INTEGER, truncated INTEGER DEFAULT 0, meta TEXT);
CREATE TABLE IF NOT EXISTS files (
  file_id INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_id TEXT, session_id INTEGER, direction TEXT,
  filename TEXT, local_path TEXT, cloud_path TEXT,
  size_bytes INTEGER, sha256 TEXT, mime TEXT,
  status TEXT, transferred_at TEXT, note TEXT,
  UNIQUE(machine_id, cloud_path, sha256));
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, machine_id TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS enroll_tickets (
  ticket TEXT PRIMARY KEY, desc TEXT, machine_id TEXT,
  control_port INTEGER, serve_port INTEGER, guard_base INTEGER,
  created TEXT, expires_ts REAL, ttl_min INTEGER,
  status TEXT, used_at TEXT, used_by TEXT);
CREATE TABLE IF NOT EXISTS runtime_profiles (
  machine_id TEXT PRIMARY KEY, runtime TEXT, action TEXT, action_tried TEXT,
  features TEXT, timings TEXT, samples TEXT, source TEXT, probed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_sessions_machine ON sessions(machine_id, started_at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_files_machine ON files(machine_id, transferred_at);
CREATE VIEW IF NOT EXISTS v_agent_activity AS
SELECT a.machine_id, a.hostname, a.desc AS agent_desc, a.status,
 (SELECT COUNT(*) FROM sessions s WHERE s.machine_id=a.machine_id) AS sessions,
 (SELECT COUNT(*) FROM messages m WHERE m.session_id IN
    (SELECT session_id FROM sessions s2 WHERE s2.machine_id=a.machine_id)) AS messages,
 (SELECT COUNT(*) FROM files f WHERE f.machine_id=a.machine_id) AS files,
 (SELECT IFNULL(SUM(f.size_bytes),0) FROM files f WHERE f.machine_id=a.machine_id) AS bytes,
 (SELECT MAX(started_at) FROM sessions s3 WHERE s3.machine_id=a.machine_id) AS last_session,
 a.last_seen
FROM agents a;
"""


# ---------- 连接 ----------
def connect(db=DB):
    os.makedirs(os.path.dirname(db), exist_ok=True)
    con = sqlite3.connect(db, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _migrate(con):
    """轻量迁移：给老库补列（幂等）。"""
    for tbl, col in (("agents", "runtime"), ("agents", "agent_action"),
                     ("sessions", "runtime"), ("sessions", "action")):
        cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % tbl)]
        if col not in cols:
            con.execute("ALTER TABLE %s ADD COLUMN %s TEXT" % (tbl, col))
            print("[agentsdb] 迁移: %s.%s 已添加" % (tbl, col))


def init(db=DB):
    con = connect(db)
    con.executescript(SCHEMA)
    _migrate(con)
    con.commit()
    return con


def save_profile(profile):
    """写入/更新运行时画像（探测或自述的结果）。"""
    con = init()
    con.execute("""INSERT OR REPLACE INTO runtime_profiles
        (machine_id,runtime,action,action_tried,features,timings,samples,source,probed_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (profile.get("machine_id"), profile.get("runtime"), profile.get("action"),
         json.dumps(profile.get("action_tried"), ensure_ascii=False),
         json.dumps(profile.get("features"), ensure_ascii=False),
         json.dumps(profile.get("timings"), ensure_ascii=False),
         json.dumps(profile.get("samples"), ensure_ascii=False),
         profile.get("source"), profile.get("probed_at")))
    con.commit()
    con.close()


def get_profile(machine_id):
    """读画像 → dict（含 action / features / timings）；没有则 {}"""
    con = init()
    r = con.execute("SELECT * FROM runtime_profiles WHERE machine_id=?", (machine_id,)).fetchone()
    con.close()
    if not r:
        return {}
    d = dict(r)
    for k in ("action_tried", "features", "timings", "samples"):
        try:
            d[k] = json.loads(d[k] or "null") or {}
        except Exception:
            d[k] = {}
    return d


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------- 写入 API ----------
def upsert_agent(machine_id, **kw):
    con = init()
    cols = ["hostname", "desc", "platform", "control_port", "serve_port", "guard_base",
            "token_key", "env_path", "method", "registered_at", "last_seen", "status", "meta"]
    cur = con.execute("SELECT machine_id FROM agents WHERE machine_id=?", (machine_id,))
    if cur.fetchone():
        sets = ", ".join("%s=?" % c for c in cols if c in kw)
        vals = [kw[c] for c in cols if c in kw]
        if sets:
            con.execute("UPDATE agents SET %s WHERE machine_id=?" % sets, vals + [machine_id])
    else:
        con.execute("INSERT INTO agents (machine_id,%s) VALUES (?,%s)"
                    % (",".join(cols), ",".join(["?"] * len(cols))),
                    [machine_id] + [kw.get(c) for c in cols])
    con.commit()
    con.close()


def log_session(machine_id, kind, title=None, status="running", cwd=None,
                duration_s=None, summary=None, meta=None, started_at=None, session_id=None):
    """写入/更新一条会话，返回 session_id。"""
    con = init()
    if session_id:
        con.execute("""UPDATE sessions SET status=?, ended_at=?, duration_s=?, summary=?
                       WHERE session_id=?""", (status, now(), duration_s, summary, session_id))
        con.commit()
        con.close()
        return session_id
    cur = con.execute("""INSERT INTO sessions
        (machine_id,kind,title,started_at,status,cwd,duration_s,summary,meta)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (machine_id, kind, title, started_at or now(), status, cwd, duration_s, summary,
         json.dumps(meta, ensure_ascii=False) if meta else None))
    sid = cur.lastrowid
    con.commit()
    con.close()
    return sid


def log_message(session_id, role, content, meta=None):
    content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    trunc = 0
    if len(content) > MAX_MSG:
        content = content[:MAX_MSG]
        trunc = 1
    con = init()
    con.execute("""INSERT INTO messages (session_id,ts,role,content,content_len,truncated,meta)
                   VALUES (?,?,?,?,?,?,?)""",
                (session_id, now(), role, content, len(content), trunc,
                 json.dumps(meta, ensure_ascii=False) if meta else None))
    con.commit()
    con.close()


def log_file(machine_id, direction, cloud_path, local_path=None, filename=None,
             size_bytes=None, sha256=None, mime=None, status="recorded", note=None,
             session_id=None, transferred_at=None):
    con = init()
    filename = filename or os.path.basename((cloud_path or local_path or "").replace("\\", "/"))
    try:
        con.execute("""INSERT OR REPLACE INTO files
            (machine_id,session_id,direction,filename,local_path,cloud_path,size_bytes,sha256,
             mime,status,transferred_at,note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (machine_id, session_id, direction, filename, local_path, cloud_path,
             size_bytes, sha256, mime, status, transferred_at or now(), note))
        con.commit()
    except sqlite3.Error as e:
        print("[agentsdb] log_file 失败: %s" % e, file=sys.stderr)
    con.close()


def log_event(machine_id, event, detail=None):
    con = init()
    con.execute("INSERT INTO events (ts,machine_id,event,detail) VALUES (?,?,?,?)",
                (now(), machine_id, event,
                 detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)))
    con.commit()
    con.close()


def sha256_file(path, limit=None):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
            if limit and n >= limit:
                break
    return h.hexdigest()


# ---------- 同步/导入 ----------
def sync_registry():
    reg = json.load(open(REGISTRY, encoding="utf-8")).get("machines", {})
    for mid, m in reg.items():
        upsert_agent(mid, hostname=m.get("hostname"), desc=m.get("desc"),
                     control_port=m.get("control_port"), serve_port=m.get("serve_port"),
                     guard_base=m.get("guard_base"), token_key=m.get("token_key"),
                     env_path=m.get("env"), method=m.get("method"),
                     registered_at=m.get("registered_at"))
    print("已同步 %d 个代理: %s" % (len(reg), ", ".join(reg)))


def import_audit():
    n_ev = n_tk = 0
    p = os.path.join(ENROLL, "audit.jsonl")
    if os.path.exists(p):
        con = init()
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            con.execute("INSERT INTO events (ts,machine_id,event,detail) VALUES (?,?,?,?)",
                        (r.get("ts"), r.get("machine_id"), r.get("event"),
                         json.dumps(r, ensure_ascii=False)))
            n_ev += 1
        con.commit()
        con.close()
    p = os.path.join(ENROLL, "tickets.json")
    if os.path.exists(p):
        t = json.load(open(p, encoding="utf-8")).get("tickets", {})
        con = init()
        for tid, r in t.items():
            con.execute("""INSERT OR REPLACE INTO enroll_tickets
                (ticket,desc,machine_id,control_port,serve_port,guard_base,created,
                 expires_ts,ttl_min,status,used_at,used_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, r.get("desc"), r.get("machine_id"), r.get("control_port"),
                 r.get("serve_port"), r.get("guard_base"), r.get("created"),
                 r.get("expires_ts"), r.get("ttl_min"), r.get("status"),
                 r.get("used_at"), r.get("used_by")))
            n_tk += 1
        con.commit()
        con.close()
    print("已导入 events=%d 条、凭证=%d 个" % (n_ev, n_tk))


def scan_uploads(directory, machine_id="machine-a", recursive=True):
    n = 0
    SKIP = (".sshpass", ".agent_token", ".reasonix_token", "admin.key", ".github_token")
    for root, dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(SKIP) or f in ("env.sh", "env.sh.bak") or f.endswith(".token"):
                continue                                   # 密件不入台账（只记元数据也不行）
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            h = sha256_file(p)
            log_file(machine_id, "upload", cloud_path=p, size_bytes=sz, sha256=h,
                     status="indexed", note="scan-uploads",
                     transferred_at=time.strftime("%Y-%m-%d %H:%M:%S",
                                                  time.localtime(os.path.getmtime(p))))
            n += 1
        if not recursive:
            break
    print("已索引 %d 个文件（目录 %s）" % (n, directory))


# ---------- 查询 ----------
def show_sessions(machine=None, n=20):
    con = init()
    q = """SELECT s.session_id, s.machine_id, s.kind, s.title, s.status, s.started_at,
                  s.duration_s, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id) AS msgs
           FROM sessions s %s ORDER BY s.session_id DESC LIMIT ?"""
    rows = con.execute(q % ("WHERE s.machine_id=?" if machine else ""),
                       ([machine, n] if machine else [n])).fetchall()
    print("%-6s %-10s %-14s %-30s %-6s %-20s %s" %
          ("会话", "代理", "类型", "标题", "状态", "开始", "消息数"))
    for r in rows:
        print("%-6s %-10s %-14s %-30s %-6s %-20s %s" %
              (r["session_id"], r["machine_id"], r["kind"], (r["title"] or "")[:28],
               r["status"], r["started_at"], r["msgs"]))
    return rows


def show_files(machine=None, n=20):
    con = init()
    rows = con.execute("""SELECT file_id,machine_id,direction,filename,size_bytes,status,transferred_at
                          FROM files %s ORDER BY file_id DESC LIMIT ?"""
                       % ("WHERE machine_id=?" if machine else ""),
                       ([machine, n] if machine else [n])).fetchall()
    print("%-5s %-10s %-9s %-38s %12s %-9s %s" %
          ("ID", "代理", "方向", "文件", "大小", "状态", "时间"))
    for r in rows:
        print("%-5s %-10s %-9s %-38s %12s %-9s %s" %
              (r["file_id"], r["machine_id"], r["direction"], (r["filename"] or "")[:36],
               ("%.2f MB" % (r["size_bytes"] / 1048576)) if r["size_bytes"] else "-",
               r["status"], r["transferred_at"]))
    return rows


def show_stats():
    con = init()
    print("=== 每个代理的活动汇总 ===")
    rows = con.execute("SELECT * FROM v_agent_activity").fetchall()
    print("%-10s %-22s %-8s %-8s %-6s %-12s %s" %
          ("代理", "说明", "会话", "消息", "文件", "传输量", "最近会话"))
    for r in rows:
        print("%-10s %-22s %-8s %-8s %-6s %-12s %s" %
              (r["machine_id"], (r["agent_desc"] or "")[:20], r["sessions"], r["messages"],
               r["files"], "%.1f MB" % (r["bytes"] / 1048576), r["last_session"] or "-"))
    tot = con.execute("""SELECT (SELECT COUNT(*) FROM sessions) a, (SELECT COUNT(*) FROM messages) b,
                                (SELECT COUNT(*) FROM files) c, (SELECT COUNT(*) FROM events) d,
                                (SELECT COUNT(*) FROM enroll_tickets) e""").fetchone()
    print("\n库内总计: 会话 %d ｜ 消息 %d ｜ 文件 %d ｜ 事件 %d ｜ 凭证 %d ｜ 库大小 %.0f KB"
          % (tot["a"], tot["b"], tot["c"], tot["d"], tot["e"],
             os.path.getsize(DB) / 1024 if os.path.exists(DB) else 0))


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=lambda a: (init(), print("库已就绪: %s" % DB)) and 0)
    sub.add_parser("sync-registry").set_defaults(fn=lambda a: sync_registry() or 0)
    sub.add_parser("import-audit").set_defaults(fn=lambda a: import_audit() or 0)
    p = sub.add_parser("scan-uploads")
    p.add_argument("directory")
    p.add_argument("--machine", default="machine-a")
    p.set_defaults(fn=lambda a: scan_uploads(a.directory, a.machine) or 0)
    p = sub.add_parser("log-session")
    p.add_argument("--machine", required=True)
    p.add_argument("--kind", default="manual")
    p.add_argument("--title", default="")
    p.add_argument("--status", default="ok")
    p.add_argument("--duration", type=float, default=None)
    p.set_defaults(fn=lambda a: print("session_id=%d" % log_session(
        a.machine, a.kind, title=a.title, status=a.status, duration_s=a.duration)) or 0)
    p = sub.add_parser("log-message")
    p.add_argument("--session", type=int, required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--content", default="")
    p.add_argument("--content-file")
    p.set_defaults(fn=lambda a: log_message(a.session, a.role,
                   a.content or open(a.content_file, encoding="utf-8").read()) or print("ok") or 0)
    p = sub.add_parser("log-file")
    p.add_argument("--machine", required=True)
    p.add_argument("--direction", required=True)
    p.add_argument("--cloud-path", required=True)
    p.add_argument("--local-path")
    p.add_argument("--session", type=int)
    p.add_argument("--note")
    p.set_defaults(fn=lambda a: log_file(a.machine, a.direction, a.cloud_path,
                   local_path=a.local_path, session_id=a.session, note=a.note) or print("ok") or 0)
    p = sub.add_parser("sessions")
    p.add_argument("--machine")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=lambda a: show_sessions(a.machine, a.n) and 0)
    p = sub.add_parser("files")
    p.add_argument("--machine")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=lambda a: show_files(a.machine, a.n) and 0)
    sub.add_parser("stats").set_defaults(fn=lambda a: show_stats() or 0)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

"""居民需求与推进计划的本地持久化边界。

存储分三层：
- ``community_need``：最早版本保留的通用登记记录（基线兼容）。
- ``event_log``：只追加的领域事件，带前后串联哈希，是状态的唯一事实来源。
- ``snapshot_store``：每个决定使用的输入快照（内容寻址，编号确定）。
- ``command_log``：收到的命令与结果，支持幂等重放。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .domain import Record
from .events import GENESIS_HASH, Event, make_event_hash


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS community_need (
                record_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS event_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                snapshot_id TEXT NOT NULL DEFAULT '',
                prev_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_store (
                snapshot_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS command_log (
                command_id TEXT PRIMARY KEY,
                command_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.connection.commit()

    # -- 基线记录 ---------------------------------------------------------

    def save(self, record: Record) -> Record:
        value = record.with_timestamp()
        self.connection.execute(
            "INSERT INTO community_need(record_id, owner_id, state, created_at) VALUES(?,?,?,?)",
            (value.record_id, value.owner_id, value.state, value.created_at),
        )
        self.connection.commit()
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, created_at FROM community_need WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # -- 事件日志 ---------------------------------------------------------

    def append_event(self, event_type: str, payload: dict, created_at: str,
                     snapshot_id: str = "") -> Event:
        row = self.connection.execute(
            "SELECT event_hash FROM event_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["event_hash"] if row else GENESIS_HASH
        event_hash = make_event_hash(prev_hash, event_type, payload, created_at)
        cur = self.connection.execute(
            """INSERT INTO event_log(event_type, payload, created_at, snapshot_id,
                                     prev_hash, event_hash)
               VALUES(?,?,?,?,?,?)""",
            (event_type, json.dumps(payload, ensure_ascii=False), created_at,
             snapshot_id, prev_hash, event_hash),
        )
        self.connection.commit()
        return Event(seq=cur.lastrowid, event_type=event_type, payload=payload,
                     created_at=created_at, snapshot_id=snapshot_id,
                     prev_hash=prev_hash, event_hash=event_hash)

    def iter_events(self) -> list[Event]:
        rows = self.connection.execute(
            """SELECT seq, event_type, payload, created_at, snapshot_id,
                      prev_hash, event_hash
               FROM event_log ORDER BY seq"""
        ).fetchall()
        return [Event(seq=r["seq"], event_type=r["event_type"],
                      payload=json.loads(r["payload"]),
                      created_at=r["created_at"], snapshot_id=r["snapshot_id"],
                      prev_hash=r["prev_hash"], event_hash=r["event_hash"])
                for r in rows]

    def verify_chain(self) -> bool:
        """重算整条哈希链。任何对历史事件的悄悄改动都会返回 False。"""
        prev_hash = GENESIS_HASH
        for event in self.iter_events():
            if event.prev_hash != prev_hash:
                return False
            if make_event_hash(prev_hash, event.event_type, event.payload,
                               event.created_at) != event.event_hash:
                return False
            prev_hash = event.event_hash
        return True

    # -- 输入快照 ---------------------------------------------------------

    def save_snapshot(self, snapshot_id: str, kind: str, content: dict,
                      created_at: str) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO snapshot_store(snapshot_id, kind, content, created_at)
               VALUES(?,?,?,?)""",
            (snapshot_id, kind, json.dumps(content, ensure_ascii=False), created_at),
        )
        self.connection.commit()

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT content FROM snapshot_store WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        return json.loads(row["content"]) if row else None

    # -- 命令日志（重放） --------------------------------------------------

    def command_exists(self, command_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM command_log WHERE command_id=?", (command_id,)
        ).fetchone() is not None

    def get_command_result(self, command_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT result FROM command_log WHERE command_id=?", (command_id,)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def log_command(self, command_id: str, command_type: str, actor_id: str,
                    payload: dict, result: dict, created_at: str) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO command_log(command_id, command_type, actor_id,
                                                  payload, result, created_at)
               VALUES(?,?,?,?,?,?)""",
            (command_id, command_type, actor_id,
             json.dumps(payload, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), created_at),
        )
        self.connection.commit()

    def iter_commands(self) -> list[dict]:
        rows = self.connection.execute(
            """SELECT command_id, command_type, actor_id, payload, result, created_at
               FROM command_log ORDER BY rowid"""
        ).fetchall()
        return [{"command_id": r["command_id"], "command_type": r["command_type"],
                 "actor_id": r["actor_id"], "payload": json.loads(r["payload"]),
                 "result": json.loads(r["result"]), "created_at": r["created_at"]}
                for r in rows]

    def close(self) -> None:
        self.connection.close()

"""追加事件、输入快照与链式哈希。

设计要点：
- 事件只追加、不更新、不删除；当前状态由事件重放得到。
- 事件载荷使用规范化 JSON 计算哈希，并与上一条事件哈希串联，
  任何事后篡改（例如冻结后改分）都会破坏哈希链。
- “输入快照”保存某个决定实际使用的全部输入（诉求、权重、预算等），
  快照内容不含墙钟时间，因此命令重放后会得到相同的快照编号，
  查询时可逐项核对决定依据。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


def canonical(obj) -> str:
    """规范化 JSON 序列化：键排序、无空白、非 ASCII 原样保留。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def content_hash(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    seq: int
    event_type: str
    payload: dict
    created_at: str
    snapshot_id: str
    prev_hash: str
    event_hash: str


def make_event_hash(prev_hash: str, event_type: str, payload: dict,
                    created_at: str) -> str:
    material = canonical({
        "prev_hash": prev_hash,
        "event_type": event_type,
        "payload": payload,
        "created_at": created_at,
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


GENESIS_HASH = "0" * 64


def new_snapshot_id(kind: str, content: dict) -> str:
    """快照编号 = 内容哈希的短前缀，重放时确定性复现。"""
    return f"snap_{kind}_{content_hash(content)[:16]}"

"""供进程内调用的轻量请求适配层。

请求形如 ``{"action": "...", "actor_id": "...", ...}``。
写操作走统一命令分发（:meth:`Service.dispatch``），可携带 ``command_id``
实现幂等；查询动作直接返回只读结果。
"""
from __future__ import annotations

import json

from .service import Service

_QUERIES = {
    "get_demand", "list_candidates", "list_projects", "get_project",
    "list_decisions", "get_decision", "list_notifications", "cycle_status",
    "verify_integrity", "replay_command_log",
}


def handle(payload: str, service: Service | None = None) -> str:
    service = service or Service()
    body = json.loads(payload)
    action = body.get("action")

    if action == "health":
        return json.dumps(service.health(), ensure_ascii=False)
    if action == "register":  # 基线兼容
        return json.dumps(
            service.register(str(body["record_id"]), str(body["owner_id"])),
            ensure_ascii=False)
    if action == "find":  # 基线兼容
        return json.dumps(service.find(str(body["record_id"])), ensure_ascii=False)

    data = {k: v for k, v in body.items()
            if k not in ("action", "actor_id", "command_id", "at")}

    if action in _QUERIES:
        result = _dispatch_query(service, action, data)
        return json.dumps(result, ensure_ascii=False)

    if action is None:
        raise ValueError("不支持的请求动作")
    result = service.dispatch(
        action, str(body.get("actor_id", "")), data,
        command_id=str(body.get("command_id", "")), at=body.get("at"))
    return json.dumps(result, ensure_ascii=False)


def _dispatch_query(service: Service, action: str, data: dict):
    if action == "get_demand":
        return service.get_demand(data["demand_id"])
    if action == "list_candidates":
        return service.list_candidates(data["cycle_id"])
    if action == "list_projects":
        return service.list_projects(data.get("cycle_id"), data.get("status"))
    if action == "get_project":
        return service.get_project(data["project_id"])
    if action == "list_decisions":
        return service.list_decisions(data["cycle_id"])
    if action == "get_decision":
        return service.get_decision(data["project_id"])
    if action == "list_notifications":
        return service.list_notifications(data.get("community_id"))
    if action == "cycle_status":
        return service.cycle_status(data["cycle_id"])
    if action == "verify_integrity":
        return service.verify_integrity()
    if action == "replay_command_log":
        return service.replay_command_log()
    raise ValueError(f"不支持的查询动作：{action}")

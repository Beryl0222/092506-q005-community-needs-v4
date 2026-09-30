"""居民需求采集与年度决策的应用服务。

业务主线
--------
1. 居民代表提交“带证据的缺口”（:meth:`submit_demand`），重复诉求不会被丢弃，
   而是由社区合并（:meth:`merge_demands`），合并后人口覆盖累加。
2. 街道开启年度周期并给出两年预算（:meth:`open_cycle`），服务按
   “人口覆盖 0.5 / 紧急程度 0.3 / 老幼服务 0.2”给候选项目打分
   （:meth:`score_candidates`），评分使用的全部输入固化为内容寻址快照。
3. 按分数贪心排入两年计划，预算不足的项目显式递延（:meth:`build_plan`）。
4. 项目须经街道、商户两级确认（:meth:`confirm_project`）后方可冻结预算
   （:meth:`freeze_budget`）。冻结后评分与计划不可再变；审批人变更
   （:meth:`change_approver`）只记录责任人，绝不改分。
5. 撤回或延期必须说明影响并通知相关社区（:meth:`withdraw_project` /
   :meth:`delay_project`）。
6. :meth:`replay_command_log` 可把全部命令重放进一个空库，重算出的事件哈希、
   快照编号与当前状态必须完全一致；:meth:`verify_integrity` 用于发现任何篡改。
"""
from __future__ import annotations

from uuid import uuid4

from .domain import (
    Demand,
    Evidence,
    Record,
    ScoreCard,
    now_iso,
)
from .events import Event, canonical, new_snapshot_id
from .store import Store

DEFAULT_WEIGHTS = {"population": 0.5, "urgency": 0.3, "vulnerable": 0.2}


class Service:
    def __init__(self, store: Store | None = None, clock=None) -> None:
        self.store = store or Store()
        self.clock = clock or now_iso
        self._load()

    # ------------------------------------------------------------------ #
    # 状态归约（事件重放）
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        self.demands: dict[str, dict] = {}
        self.cycles: dict[str, dict] = {}
        self.score_runs: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.notifications: list[dict] = []
        for event in self.store.iter_events():
            self._apply(event)

    def _emit(self, event_type: str, payload: dict, at: str | None = None,
              snapshot_id: str = "") -> Event:
        event = self.store.append_event(
            event_type, payload, at or self.clock(), snapshot_id=snapshot_id)
        self._apply(event)
        return event

    def _apply(self, event: Event) -> None:
        p = event.payload
        kind = event.event_type
        if kind == "demand_submitted":
            self.demands[p["demand_id"]] = dict(p)
        elif kind == "demands_merged":
            parent = self.demands[p["parent_id"]]
            child = self.demands[p["child_id"]]
            child["merged_into"] = parent["demand_id"]
            merged = [p["child_id"], *child.get("supporting_demands", [])]
            parent.setdefault("supporting_demands", [])
            for did in merged:
                if did not in parent["supporting_demands"]:
                    parent["supporting_demands"].append(did)
        elif kind == "cycle_opened":
            self.cycles[p["cycle_id"]] = dict(p)
        elif kind == "candidates_scored":
            self.score_runs[p["cycle_id"]] = p
        elif kind == "plan_built":
            plan = self.plans.get(p["cycle_id"])
            if plan:  # 冻结前允许重建计划：先移除旧版本项目
                for proj in plan["projects"]:
                    self.projects.pop(proj["project_id"], None)
            self.plans[p["cycle_id"]] = p
            for proj in p["projects"]:
                self.projects[proj["project_id"]] = dict(proj)
        elif kind == "budget_frozen":
            self.cycles[p["cycle_id"]]["frozen"] = True
            self.cycles[p["cycle_id"]]["frozen_at"] = p["frozen_at"]
        elif kind == "project_confirmed":
            proj = self.projects[p["project_id"]]
            if p["level"] == "street":
                proj["street_confirmed"] = True
                proj["street_confirmed_by"] = p["approver_id"]
            else:
                proj["merchant_confirmed"] = True
                proj["merchant_confirmed_by"] = p["approver_id"]
            if proj["street_confirmed"] and proj["merchant_confirmed"] and \
                    proj["status"] == "proposed":
                proj["status"] = "active"
            proj["updated_at"] = event.created_at
        elif kind == "approver_changed":
            proj = self.projects[p["project_id"]]
            key = ("street_confirmed_by" if p["level"] == "street"
                   else "merchant_confirmed_by")
            proj[key] = p["new_approver_id"]
            proj["updated_at"] = event.created_at
        elif kind == "project_withdrawn":
            proj = self.projects[p["project_id"]]
            proj["status"] = "withdrawn"
            proj["status_reason"] = p["reason"]
            proj["affected_communities"] = p["impact"]["affected_communities"]
            proj["notified_communities"] = sorted(set(
                proj.get("notified_communities", []) + p["notified_communities"]))
            proj["updated_at"] = event.created_at
        elif kind == "project_delayed":
            proj = self.projects[p["project_id"]]
            proj["status"] = "delayed"
            proj["status_reason"] = p["reason"]
            proj["plan_year"] = p["to_year"]
            proj["affected_communities"] = p["impact"]["affected_communities"]
            proj["notified_communities"] = sorted(set(
                proj.get("notified_communities", []) + p["notified_communities"]))
            proj["updated_at"] = event.created_at
        elif kind == "project_completed":
            proj = self.projects[p["project_id"]]
            proj["status"] = "completed"
            proj["updated_at"] = event.created_at
        elif kind == "communities_notified":
            self.notifications.append(dict(p, notified_at=event.created_at))

    # ------------------------------------------------------------------ #
    # 基线兼容
    # ------------------------------------------------------------------ #

    def health(self) -> dict[str, str]:
        return {"service": "community_needs", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str]:
        record = self.store.save(Record(record_id, owner_id))
        return {"record_id": record.record_id, "owner_id": record.owner_id,
                "state": record.state, "created_at": record.created_at}

    def find(self, record_id: str) -> dict[str, str] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # ------------------------------------------------------------------ #
    # 阶段一：需求采集与社区合并
    # ------------------------------------------------------------------ #

    def submit_demand(self, demand_id: str, community_id: str, submitter_id: str,
                      facility_type: str, description: str,
                      evidence: list[dict], estimated_beneficiaries: int = 0,
                      age_groups: list[str] | None = None, urgency: int = 3,
                      elderly_covered: int | None = None,
                      child_covered: int | None = None,
                      at: str | None = None) -> dict:
        """居民代表提交带证据的缺口。

        重复提交不会覆盖原始诉求；返回 ``similar_demands`` 提示可合并的相似项，
        是否合并由 :meth:`merge_demands` 显式决定。
        """
        if demand_id in self.demands:
            return self._idempotent_submit(demand_id)
        if not evidence:
            raise ValueError("诉求必须附带至少一条证据")
        if not 1 <= urgency <= 5:
            raise ValueError("紧急程度必须在 1-5 之间")
        if estimated_beneficiaries < 0:
            raise ValueError("覆盖人口不能为负")
        age_groups = sorted(set(age_groups or []))
        ev_items = tuple(
            Evidence(str(e["evidence_id"]), str(e.get("kind", "other")),
                     str(e.get("description", "")), str(e.get("uri", "")))
            for e in evidence)
        if elderly_covered is None:
            elderly_covered = estimated_beneficiaries if "elderly" in age_groups else 0
        if child_covered is None:
            child_covered = estimated_beneficiaries if "child" in age_groups else 0
        demand = Demand(
            demand_id=demand_id, community_id=community_id,
            submitter_id=submitter_id, facility_type=facility_type,
            description=description, evidence=ev_items,
            estimated_beneficiaries=estimated_beneficiaries,
            age_groups=tuple(age_groups), urgency=urgency,
            created_at=at or self.clock())
        payload = self._demand_payload(demand, elderly_covered, child_covered)
        similar = sorted(
            d["demand_id"] for d in self.demands.values()
            if not d.get("merged_into") and d["facility_type"] == facility_type)
        event = self._emit("demand_submitted", payload, at=at)
        result = {"demand_id": demand_id, "state": "submitted",
                  "similar_demands": similar,
                  "event_seq": event.seq}
        return result

    def _idempotent_submit(self, demand_id: str) -> dict:
        return {"demand_id": demand_id, "state": "already_submitted",
                "similar_demands": [], "deduplicated": True}

    @staticmethod
    def _demand_payload(demand: Demand, elderly_covered: int,
                        child_covered: int) -> dict:
        return {
            "demand_id": demand.demand_id,
            "community_id": demand.community_id,
            "submitter_id": demand.submitter_id,
            "facility_type": demand.facility_type,
            "description": demand.description,
            "estimated_beneficiaries": demand.estimated_beneficiaries,
            "age_groups": list(demand.age_groups),
            "urgency": demand.urgency,
            "elderly_covered": elderly_covered,
            "child_covered": child_covered,
            "evidence": [
                {"evidence_id": e.evidence_id, "kind": e.kind,
                 "description": e.description, "uri": e.uri}
                for e in sorted(demand.evidence, key=lambda e: e.evidence_id)],
            "created_at": demand.created_at,
            "merged_into": "",
            "supporting_demands": [],
        }

    def merge_demands(self, parent_id: str, child_id: str, merged_by: str,
                      reason: str = "", at: str | None = None) -> dict:
        """社区把相似诉求合并进主诉求；人口覆盖在评分时按合并组累加。"""
        if parent_id == child_id:
            raise ValueError("不能把诉求合并进自身")
        parent = self.demands.get(parent_id)
        child = self.demands.get(child_id)
        if not parent or not child:
            raise ValueError("诉求不存在")
        if child.get("merged_into"):
            raise ValueError(f"诉求 {child_id} 已被合并，不能重复合并")
        if parent.get("merged_into"):
            raise ValueError(f"诉求 {parent_id} 自身已被合并，请合并到其主诉求")
        if parent["facility_type"] != child["facility_type"]:
            raise ValueError("只能合并同类业态的相似诉求")
        payload = {"parent_id": parent_id, "child_id": child_id,
                   "merged_by": merged_by, "reason": reason,
                   "community_id": parent["community_id"]}
        event = self._emit("demands_merged", payload, at=at)
        result = {"parent_id": parent_id,
                  "supporting_demands": list(parent["supporting_demands"]),
                  "event_seq": event.seq}
        return result

    def get_demand(self, demand_id: str) -> dict | None:
        demand = self.demands.get(demand_id)
        if not demand:
            return None
        group_ids = [demand_id, *demand.get("supporting_demands", [])]
        covered = sum(self.demands[i]["estimated_beneficiaries"] for i in group_ids)
        return dict(demand, group_demand_ids=group_ids,
                    group_population_covered=covered)

    # ------------------------------------------------------------------ #
    # 阶段二：年度周期、评分与两年计划
    # ------------------------------------------------------------------ #

    def open_cycle(self, cycle_id: str, year: int, total_budget: int = 0,
                   year1_budget: int | None = None,
                   year2_budget: int | None = None,
                   weights: dict | None = None, opened_by: str = "",
                   at: str | None = None) -> dict:
        if cycle_id in self.cycles:
            raise ValueError(f"周期 {cycle_id} 已存在")
        if year1_budget is None or year2_budget is None:
            year1_budget = total_budget // 2
            year2_budget = total_budget - year1_budget
        if year1_budget < 0 or year2_budget < 0:
            raise ValueError("预算不能为负")
        w = dict(DEFAULT_WEIGHTS, **(weights or {}))
        if abs(sum(w.values()) - 1.0) > 1e-9:
            raise ValueError("评分权重之和必须为 1")
        payload = {"cycle_id": cycle_id, "year": year,
                   "year1_budget": year1_budget, "year2_budget": year2_budget,
                   "weights": w, "frozen": False, "frozen_at": "",
                   "opened_by": opened_by}
        event = self._emit("cycle_opened", payload, at=at)
        result = {"cycle_id": cycle_id, "year": year,
                  "year1_budget": year1_budget, "year2_budget": year2_budget,
                  "weights": w, "event_seq": event.seq}
        return result

    def _root_groups(self, cycle_id: str | None = None) -> list[dict]:
        """返回所有未被合并的主诉求及其合并组聚合数据。

        指定 ``cycle_id`` 时，已在更早年度周期进入 active/completed 项目的
        诉求视为“已补齐”，不再进入新周期候选；被撤回或延期的诉求可重新参评。
        """
        cycle_year = self.cycles[cycle_id]["year"] if cycle_id else None
        addressed = set()
        if cycle_year is not None:
            for proj in self.projects.values():
                prior_year = self.cycles[proj["cycle_id"]]["year"]
                if prior_year < cycle_year and proj["status"] in (
                        "active", "completed", "proposed"):
                    addressed.add(proj["demand_id"])
        groups = []
        for demand in self.demands.values():
            if demand.get("merged_into"):
                continue
            if demand["demand_id"] in addressed:
                continue
            members = [demand, *(self.demands[i]
                                 for i in demand.get("supporting_demands", []))]
            communities = sorted({m["community_id"] for m in members})
            groups.append({
                "root": demand,
                "members": members,
                "communities": communities,
                "population_covered": sum(m["estimated_beneficiaries"]
                                          for m in members),
                "elderly_covered": sum(m.get("elderly_covered", 0)
                                       for m in members),
                "child_covered": sum(m.get("child_covered", 0) for m in members),
                "urgency": max(m["urgency"] for m in members),
            })
        return groups

    @staticmethod
    def _demand_view(d: dict) -> dict:
        return {
            "demand_id": d["demand_id"],
            "community_id": d["community_id"],
            "submitter_id": d["submitter_id"],
            "facility_type": d["facility_type"],
            "description": d["description"],
            "estimated_beneficiaries": d["estimated_beneficiaries"],
            "age_groups": sorted(d["age_groups"]),
            "urgency": d["urgency"],
            "elderly_covered": d.get("elderly_covered", 0),
            "child_covered": d.get("child_covered", 0),
            "evidence": sorted(d.get("evidence", []),
                               key=lambda e: e["evidence_id"]),
            "supporting_demands": sorted(d.get("supporting_demands", [])),
            "merged_into": d.get("merged_into", ""),
        }

    def _scoring_snapshot(self, cycle: dict, groups: list[dict],
                          cost_estimates: dict) -> tuple[str, dict]:
        content = {
            "type": "scoring",
            "cycle_id": cycle["cycle_id"],
            "weights": cycle["weights"],
            "year1_budget": cycle["year1_budget"],
            "year2_budget": cycle["year2_budget"],
            "cost_estimates": dict(sorted(cost_estimates.items())),
            "groups": [
                {
                    "root_demand": self._demand_view(g["root"]),
                    "communities": g["communities"],
                    "population_covered": g["population_covered"],
                    "elderly_covered": g["elderly_covered"],
                    "child_covered": g["child_covered"],
                    "urgency": g["urgency"],
                }
                for g in sorted(groups, key=lambda g: (
                    g["root"]["facility_type"], g["root"]["demand_id"]))
            ],
        }
        return new_snapshot_id("scoring", content), content

    def score_candidates(self, cycle_id: str, cost_estimates: dict | None = None,
                         at: str | None = None) -> dict:
        """按人口覆盖、紧急程度、老幼服务权重给候选项目打分并固化输入快照。"""
        cycle = self.cycles.get(cycle_id)
        if not cycle:
            raise ValueError(f"周期 {cycle_id} 不存在")
        if cycle["frozen"]:
            raise ValueError("预算已冻结，不能重新评分")
        if cycle_id in self.plans:
            raise ValueError("计划已生成，评分结果不可更改；请开启新周期")
        cost_estimates = cost_estimates or {}
        groups = self._root_groups(cycle_id)
        if not groups:
            raise ValueError("尚无诉求可评分")
        snapshot_id, snapshot = self._scoring_snapshot(cycle, groups, cost_estimates)
        max_covered = max(g["population_covered"] for g in groups) or 1
        candidates = []
        for g in groups:
            pop = g["population_covered"]
            popc = pop / max_covered * 100
            urgc = g["urgency"] / 5 * 100
            vulnc = min(1.0, (g["elderly_covered"] + g["child_covered"]) / pop) * 100 \
                if pop else 0.0
            w = cycle["weights"]
            total = round(w["population"] * popc + w["urgency"] * urgc
                          + w["vulnerable"] * vulnc, 2)
            card = ScoreCard(
                population_component=round(popc, 2),
                urgency_component=round(urgc, 2),
                vulnerable_component=round(vulnc, 2),
                total_score=total, population_covered=pop,
                elderly_covered=g["elderly_covered"],
                child_covered=g["child_covered"],
                weight_population=w["population"], weight_urgency=w["urgency"],
                weight_vulnerable=w["vulnerable"])
            candidates.append({
                "candidate_id": f"cand_{cycle_id}_{g['root']['demand_id']}",
                "cycle_id": cycle_id,
                "source_demand_id": g["root"]["demand_id"],
                "communities": g["communities"],
                "facility_type": g["root"]["facility_type"],
                "score_card": card.breakdown(),
                "total_score": total,
                "estimated_cost": int(cost_estimates.get(g["root"]["facility_type"], 0)),
                "population_covered": pop,
                "snapshot_id": snapshot_id,
            })
        candidates.sort(key=lambda c: (-c["total_score"], c["facility_type"],
                                       c["source_demand_id"]))
        ts = at or self.clock()
        self.store.save_snapshot(snapshot_id, "scoring", snapshot, ts)
        payload = {"cycle_id": cycle_id, "scored_at": ts,
                   "snapshot_id": snapshot_id, "candidates": candidates}
        event = self._emit("candidates_scored", payload, at=ts,
                           snapshot_id=snapshot_id)
        result = {"cycle_id": cycle_id, "snapshot_id": snapshot_id,
                  "candidate_count": len(candidates),
                  "ranking": [{"candidate_id": c["candidate_id"],
                               "total_score": c["total_score"],
                               "facility_type": c["facility_type"],
                               "communities": c["communities"]}
                              for c in candidates],
                  "event_seq": event.seq}
        return result

    def build_plan(self, cycle_id: str, at: str | None = None) -> dict:
        """按分数贪心排入两年计划；预算装不下的候选显式递延。"""
        cycle = self.cycles.get(cycle_id)
        if not cycle:
            raise ValueError(f"周期 {cycle_id} 不存在")
        if cycle["frozen"]:
            raise ValueError("预算已冻结，不能重建计划")
        run = self.score_runs.get(cycle_id)
        if not run:
            raise ValueError("请先完成候选项目评分")
        prior = self.plans.get(cycle_id)
        if prior and any(
                self.projects[pid]["street_confirmed"]
                or self.projects[pid]["merchant_confirmed"]
                for pid in (p["project_id"] for p in prior["projects"])
                if pid in self.projects):
            raise ValueError("已有项目完成确认，不能重建计划；如需调整请撤回相关项目")
        y1_cap, y2_cap = cycle["year1_budget"], cycle["year2_budget"]
        y1_spend = y2_spend = 0
        projects, deferred = [], []
        plan_items = []
        for rank, c in enumerate(run["candidates"], start=1):
            cost = c["estimated_cost"]
            if y1_spend + cost <= y1_cap:
                plan_year, y1_spend = cycle["year"], y1_spend + cost
            elif y2_spend + cost <= y2_cap:
                plan_year, y2_spend = cycle["year"] + 1, y2_spend + cost
            else:
                deferred.append({
                    "candidate_id": c["candidate_id"],
                    "source_demand_id": c["source_demand_id"],
                    "facility_type": c["facility_type"],
                    "total_score": c["total_score"],
                    "estimated_cost": cost,
                    "communities": c["communities"],
                    "reason": "两年预算均无法容纳，递延待后续周期",
                })
                plan_items.append({"candidate_id": c["candidate_id"],
                                   "total_score": c["total_score"],
                                   "estimated_cost": cost, "deferred": True})
                continue
            project = {
                "project_id": f"proj_{cycle_id}_{c['source_demand_id']}",
                "cycle_id": cycle_id,
                "candidate_id": c["candidate_id"],
                "demand_id": c["source_demand_id"],
                "facility_type": c["facility_type"],
                "communities": c["communities"],
                "score": c["total_score"],
                "estimated_cost": cost,
                "plan_year": plan_year,
                "rank": rank,
                "street_confirmed": False,
                "street_confirmed_by": "",
                "merchant_confirmed": False,
                "merchant_confirmed_by": "",
                "status": "proposed",
                "status_reason": "",
                "affected_communities": [],
                "notified_communities": [],
                "snapshot_id": run["snapshot_id"],
                "updated_at": "",
            }
            projects.append(project)
            plan_items.append({"candidate_id": c["candidate_id"],
                               "project_id": project["project_id"],
                               "total_score": c["total_score"],
                               "estimated_cost": cost, "plan_year": plan_year,
                               "rank": rank, "deferred": False})
        plan_snapshot_content = {
            "type": "plan",
            "cycle_id": cycle_id,
            "year1_budget": y1_cap,
            "year2_budget": y2_cap,
            "scoring_snapshot_id": run["snapshot_id"],
            "items": plan_items,
        }
        plan_snapshot_id = new_snapshot_id("plan", plan_snapshot_content)
        ts = at or self.clock()
        self.store.save_snapshot(plan_snapshot_id, "plan",
                                 plan_snapshot_content, ts)
        payload = {"cycle_id": cycle_id, "built_at": ts,
                   "snapshot_id": plan_snapshot_id,
                   "scoring_snapshot_id": run["snapshot_id"],
                   "year1_spend": y1_spend, "year2_spend": y2_spend,
                   "projects": projects, "deferred": deferred}
        event = self._emit("plan_built", payload, at=ts,
                           snapshot_id=plan_snapshot_id)
        result = {"cycle_id": cycle_id,
                  "plan_snapshot_id": plan_snapshot_id,
                  "scoring_snapshot_id": run["snapshot_id"],
                  "year1_spend": y1_spend, "year2_spend": y2_spend,
                  "planned": [{"project_id": p["project_id"],
                               "plan_year": p["plan_year"], "rank": p["rank"],
                               "facility_type": p["facility_type"],
                               "score": p["score"]} for p in projects],
                  "deferred": deferred,
                  "event_seq": event.seq}
        return result

    # ------------------------------------------------------------------ #
    # 阶段三：两级确认、冻结、审批人变更
    # ------------------------------------------------------------------ #

    def confirm_project(self, project_id: str, level: str, approver_id: str,
                        at: str | None = None) -> dict:
        if level not in ("street", "merchant"):
            raise ValueError("确认层级必须是 street 或 merchant")
        proj = self.projects.get(project_id)
        if not proj:
            raise ValueError(f"项目 {project_id} 不存在")
        if proj["status"] == "withdrawn":
            raise ValueError("项目已撤回，不能确认")
        if level == "merchant" and not proj["street_confirmed"]:
            raise ValueError("须先完成街道级确认，商户才能确认")
        flag, by = ("street_confirmed", "street_confirmed_by") if level == "street" \
            else ("merchant_confirmed", "merchant_confirmed_by")
        if proj[flag]:
            if proj[by] == approver_id:
                return {"project_id": project_id, "level": level,
                        "state": "already_confirmed",
                        "approver_id": approver_id}
            raise ValueError("该层级已由他人确认；责任人变更请使用 change_approver")
        payload = {"project_id": project_id, "cycle_id": proj["cycle_id"],
                   "level": level, "approver_id": approver_id}
        event = self._emit("project_confirmed", payload, at=at)
        result = {"project_id": project_id, "level": level,
                  "approver_id": approver_id,
                  "street_confirmed": self.projects[project_id]["street_confirmed"],
                  "merchant_confirmed": self.projects[project_id]["merchant_confirmed"],
                  "status": self.projects[project_id]["status"],
                  "event_seq": event.seq}
        return result

    def change_approver(self, project_id: str, level: str,
                        new_approver_id: str, reason: str,
                        changed_by: str = "", at: str | None = None) -> dict:
        """审批人变更：只追加责任人变更记录，评分与计划快照保持不变。"""
        if level not in ("street", "merchant"):
            raise ValueError("确认层级必须是 street 或 merchant")
        proj = self.projects.get(project_id)
        if not proj:
            raise ValueError(f"项目 {project_id} 不存在")
        key = "street_confirmed_by" if level == "street" else "merchant_confirmed_by"
        old = proj.get(key) or ""
        if not old:
            raise ValueError("该层级尚未产生审批人，直接进行确认即可")
        if old == new_approver_id:
            raise ValueError("新审批人与当前审批人相同")
        if not reason:
            raise ValueError("审批人变更必须说明原因")
        payload = {"project_id": project_id, "cycle_id": proj["cycle_id"],
                   "level": level, "old_approver_id": old,
                   "new_approver_id": new_approver_id, "reason": reason,
                   "changed_by": changed_by}
        event = self._emit("approver_changed", payload, at=at)
        result = {"project_id": project_id, "level": level,
                  "old_approver_id": old, "new_approver_id": new_approver_id,
                  "score_snapshot_id": proj["snapshot_id"],
                  "event_seq": event.seq}
        return result

    def freeze_budget(self, cycle_id: str, frozen_by: str = "",
                      at: str | None = None) -> dict:
        """冻结年度预算。冻结后任何改分、改计划的尝试都会被拒绝。"""
        cycle = self.cycles.get(cycle_id)
        if not cycle:
            raise ValueError(f"周期 {cycle_id} 不存在")
        if cycle["frozen"]:
            return {"cycle_id": cycle_id, "state": "already_frozen",
                    "frozen_at": cycle["frozen_at"]}
        plan = self.plans.get(cycle_id)
        if not plan:
            raise ValueError("请先生成两年推进计划")
        year_window = {cycle["year"], cycle["year"] + 1}
        pending = [pid for pid, p in self.projects.items()
                   if p["cycle_id"] == cycle_id and p["status"] != "withdrawn"
                   and p["plan_year"] in year_window
                   and not (p["street_confirmed"] and p["merchant_confirmed"])]
        if pending:
            raise ValueError(f"以下项目尚未完成两级确认，不能冻结：{sorted(pending)}")
        ts = at or self.clock()
        payload = {"cycle_id": cycle_id, "frozen_at": ts, "frozen_by": frozen_by}
        event = self._emit("budget_frozen", payload, at=ts)
        result = {"cycle_id": cycle_id, "state": "frozen", "frozen_at": ts,
                  "event_seq": event.seq}
        return result

    # ------------------------------------------------------------------ #
    # 阶段四：撤回 / 延期（必须说明影响并通知社区）
    # ------------------------------------------------------------------ #

    def _impact(self, proj: dict) -> dict:
        group_ids = [proj["demand_id"]]
        demand = self.demands.get(proj["demand_id"])
        if demand:
            group_ids.extend(demand.get("supporting_demands", []))
        return {
            "affected_communities": list(proj["communities"]),
            "coverage_lost": sum(self.demands[i]["estimated_beneficiaries"]
                                 for i in group_ids if i in self.demands),
            "budget_freed": proj["estimated_cost"],
            "original_plan_year": proj["plan_year"],
            "facility_type": proj["facility_type"],
        }

    def _promotable(self, proj: dict) -> list[dict]:
        """撤回后预算空出时，给出可按原分数递补的递延候选（仅建议）。"""
        plan = self.plans.get(proj["cycle_id"])
        if not plan:
            return []
        return [{"candidate_id": d["candidate_id"], "facility_type": d["facility_type"],
                 "total_score": d["total_score"], "estimated_cost": d["estimated_cost"],
                 "fits_freed_budget": d["estimated_cost"] <= proj["estimated_cost"]}
                for d in plan["deferred"]]

    def _apply_change(self, event_type: str, proj: dict, reason: str,
                      notified_communities: list[str], extra: dict,
                      actor_id: str, at: str | None) -> dict:
        if not reason:
            raise ValueError("撤回或延期必须说明原因与影响")
        affected = set(proj["communities"])
        missing = sorted(affected - set(notified_communities))
        if missing:
            raise ValueError(f"必须通知全部受影响社区，缺少：{missing}")
        impact = self._impact(proj)
        ts = at or self.clock()
        payload = {"project_id": proj["project_id"], "cycle_id": proj["cycle_id"],
                   "reason": reason, "impact": impact,
                   "notified_communities": sorted(set(notified_communities)),
                   "actor_id": actor_id, **extra}
        event = self._emit(event_type, payload, at=ts)
        note = {
            "project_id": proj["project_id"],
            "change_type": "withdrawn" if event_type == "project_withdrawn"
            else "delayed",
            "communities": sorted(set(notified_communities)),
            "reason": reason,
            "impact": impact,
        }
        self._emit("communities_notified", note, at=ts)
        updated = self.projects[proj["project_id"]]
        result = {"project_id": proj["project_id"], "status": updated["status"],
                  "impact": impact,
                  "notified_communities": updated["notified_communities"],
                  "score_unchanged": proj["score"],
                  "score_snapshot_id": proj["snapshot_id"],
                  "promotable_deferred": self._promotable(proj),
                  "event_seq": event.seq}
        return result

    def withdraw_project(self, project_id: str, reason: str,
                         notified_communities: list[str], actor_id: str = "",
                         at: str | None = None) -> dict:
        proj = self.projects.get(project_id)
        if not proj:
            raise ValueError(f"项目 {project_id} 不存在")
        if proj["status"] == "withdrawn":
            raise ValueError("项目已处于撤回状态")
        return self._apply_change("project_withdrawn", proj, reason,
                                  notified_communities, {}, actor_id, at)

    def delay_project(self, project_id: str, to_year: int, reason: str,
                      notified_communities: list[str], actor_id: str = "",
                      at: str | None = None) -> dict:
        proj = self.projects.get(project_id)
        if not proj:
            raise ValueError(f"项目 {project_id} 不存在")
        if to_year <= proj["plan_year"]:
            raise ValueError("延期目标年度必须晚于当前计划年度")
        cycle = self.cycles[proj["cycle_id"]]
        within_window = to_year <= cycle["year"] + 1
        return self._apply_change(
            "project_delayed", proj, reason, notified_communities,
            {"from_year": proj["plan_year"], "to_year": to_year,
             "within_two_year_window": within_window},
            actor_id, at)

    # ------------------------------------------------------------------ #
    # 查询：决定依据（输入快照）与当前推进状态
    # ------------------------------------------------------------------ #

    def list_candidates(self, cycle_id: str) -> list[dict]:
        run = self.score_runs.get(cycle_id)
        if not run:
            return []
        return [dict(c, scored_at=run["scored_at"]) for c in run["candidates"]]

    def list_projects(self, cycle_id: str | None = None,
                      status: str | None = None) -> list[dict]:
        items = list(self.projects.values())
        if cycle_id:
            items = [p for p in items if p["cycle_id"] == cycle_id]
        if status:
            items = [p for p in items if p["status"] == status]
        return [dict(p) for p in
                sorted(items, key=lambda p: (p["plan_year"], p["rank"],
                                             p["project_id"]))]

    def get_project(self, project_id: str) -> dict | None:
        proj = self.projects.get(project_id)
        return dict(proj) if proj else None

    def list_decisions(self, cycle_id: str) -> list[dict]:
        """列出一个周期内每个决定：所用快照编号 + 当前推进状态。

        状态直接取事件重放后的当前投影，而不是计划生成时的项目副本，
        因此确认、变更、撤回/延期都会实时反映。
        """
        plan = self.plans.get(cycle_id)
        if not plan:
            return []
        decisions = []
        for planned in sorted(plan["projects"],
                              key=lambda p: (p["plan_year"], p["rank"])):
            p = self.projects[planned["project_id"]]
            decisions.append({
                "project_id": p["project_id"],
                "facility_type": p["facility_type"],
                "communities": p["communities"],
                "score": p["score"],
                "plan_year": p["plan_year"],
                "rank": p["rank"],
                "estimated_cost": p["estimated_cost"],
                "status": p["status"],
                "status_reason": p["status_reason"],
                "street_confirmed_by": p["street_confirmed_by"],
                "merchant_confirmed_by": p["merchant_confirmed_by"],
                "score_snapshot_id": p["snapshot_id"],
                "plan_snapshot_id": plan["snapshot_id"],
                "updated_at": p["updated_at"],
            })
        return decisions

    def get_decision(self, project_id: str) -> dict | None:
        """完整决定溯源：评分卡、两份输入快照内容、审批与变更全过程。"""
        proj = self.projects.get(project_id)
        if not proj:
            return None
        plan = self.plans[proj["cycle_id"]]
        run = self.score_runs[proj["cycle_id"]]
        candidate = next(c for c in run["candidates"]
                         if c["candidate_id"] == proj["candidate_id"])
        trail = [
            {"event_type": e.event_type, "at": e.created_at, "seq": e.seq,
             "snapshot_id": e.snapshot_id,
             "payload": e.payload if e.event_type in (
                 "project_confirmed", "approver_changed",
                 "project_withdrawn", "project_delayed") else None}
            for e in self.store.iter_events()
            if e.payload.get("project_id") == project_id
        ]
        return {
            "project": dict(proj),
            "current_status": proj["status"],
            "candidate": candidate,
            "score_card": candidate["score_card"],
            "score_snapshot_id": proj["snapshot_id"],
            "score_snapshot": self.store.get_snapshot(proj["snapshot_id"]),
            "plan_snapshot_id": plan["snapshot_id"],
            "plan_snapshot": self.store.get_snapshot(plan["snapshot_id"]),
            "decision_trail": trail,
            "notifications": [n for n in self.notifications
                              if n["project_id"] == project_id],
        }

    def list_notifications(self, community_id: str | None = None) -> list[dict]:
        if community_id:
            return [n for n in self.notifications
                    if community_id in n["communities"]]
        return list(self.notifications)

    def cycle_status(self, cycle_id: str) -> dict:
        cycle = self.cycles.get(cycle_id)
        if not cycle:
            raise ValueError(f"周期 {cycle_id} 不存在")
        run = self.score_runs.get(cycle_id)
        plan = self.plans.get(cycle_id)
        live = [p for p in self.projects.values() if p["cycle_id"] == cycle_id]

        def committed(year: int) -> int:
            return sum(p["estimated_cost"] for p in live
                       if p["plan_year"] == year
                       and p["status"] in ("proposed", "active",
                                           "delayed", "completed"))

        return {
            "cycle_id": cycle_id,
            "year": cycle["year"],
            "year1_budget": cycle["year1_budget"],
            "year2_budget": cycle["year2_budget"],
            "weights": cycle["weights"],
            "frozen": cycle["frozen"],
            "frozen_at": cycle.get("frozen_at", ""),
            "candidate_count": len(run["candidates"]) if run else 0,
            "score_snapshot_id": run["snapshot_id"] if run else "",
            "planned_count": len(plan["projects"]) if plan else 0,
            "deferred": plan["deferred"] if plan else [],
            "year1_spend": plan["year1_spend"] if plan else 0,
            "year2_spend": plan["year2_spend"] if plan else 0,
            "year1_committed": committed(cycle["year"]),
            "year2_committed": committed(cycle["year"] + 1),
            "projects": self.list_projects(cycle_id),
        }

    # ------------------------------------------------------------------ #
    # 完整性与重放
    # ------------------------------------------------------------------ #

    def verify_integrity(self) -> dict:
        """校验哈希链与快照内容；冻结后改分等篡改会在此暴露。"""
        chain_ok = self.store.verify_chain()
        snapshot_problems = []
        for event in self.store.iter_events():
            sid = event.snapshot_id
            if not sid:
                continue
            content = self.store.get_snapshot(sid)
            kind = content.get("type", "") if content else ""
            if not content or new_snapshot_id(kind, content) != sid:
                snapshot_problems.append(sid)
        return {"chain_ok": chain_ok,
                "snapshots_ok": not snapshot_problems,
                "broken_snapshots": snapshot_problems}

    def replay_command_log(self, target: Store | None = None) -> dict:
        """把已记录的命令重放进一个空库。

        重放使用原命令的时间戳与参数，因此事件哈希链、快照编号与最终投影
        必须逐字节一致；任何“悄悄改分”都会让比对失败。
        """
        target = target or Store()
        replayed = Service(target, clock=self.clock)
        commands = self.store.iter_commands()
        for rec in commands:
            replayed.dispatch(rec["command_type"], rec["actor_id"],
                              rec["payload"], command_id="", at=rec["created_at"])
        original_hashes = [e.event_hash for e in self.store.iter_events()]
        replayed_hashes = [e.event_hash for e in target.iter_events()]
        original_projection = canonical(self._projection())
        replayed_projection = canonical(replayed._projection())
        original_snapshots = {
            e.snapshot_id for e in self.store.iter_events() if e.snapshot_id}
        replayed_snapshots = {
            e.snapshot_id for e in target.iter_events() if e.snapshot_id}
        return {
            "commands_replayed": len(commands),
            "event_hashes_match": original_hashes == replayed_hashes,
            "snapshots_match": original_snapshots == replayed_snapshots,
            "projection_match": original_projection == replayed_projection,
            "original_event_count": len(original_hashes),
            "replayed_event_count": len(replayed_hashes),
        }

    def _projection(self) -> dict:
        """重放比对用的确定性投影（剔除通知事件的重复墙钟也保留原值）。"""
        return {
            "demands": {k: v for k, v in sorted(self.demands.items())},
            "cycles": {k: v for k, v in sorted(self.cycles.items())},
            "score_runs": {k: v for k, v in sorted(self.score_runs.items())},
            "plans": {k: {kk: vv for kk, vv in v.items()}
                      for k, v in sorted(self.plans.items())},
            "projects": {k: v for k, v in sorted(self.projects.items())},
            "notifications": self.notifications,
        }

    def dispatch(self, command_type: str, actor_id: str, payload: dict,
                 command_id: str = "", at: str | None = None) -> dict:
        """统一命令分发，供 API 与重放使用。

        携带 ``command_id`` 时具备幂等性：同一命令编号的重复提交直接返回首次
        结果，不再产生事件。命令日志记录完整载荷与执行时间，因此重放可以用
        完全相同的输入和时间戳重建历史。
        """
        if command_id:
            if self.store.command_exists(command_id):
                return self.store.get_command_result(command_id)
        else:
            command_id = f"cmd_{uuid4().hex}"

        def pick(*names, default=None):
            for n in names:
                if n in payload:
                    return payload[n]
            return default

        ts = at or self.clock()
        result = self._invoke(command_type, actor_id, payload, pick, ts)
        if command_id:
            self.store.log_command(command_id, command_type, actor_id,
                                   payload, result, ts)
        return result

    def _invoke(self, command_type: str, actor_id: str, payload: dict,
                pick, ts: str) -> dict:
        if command_type == "submit_demand":
            return self.submit_demand(
                pick("demand_id"), pick("community_id"), pick("submitter_id"),
                pick("facility_type"), pick("description"),
                pick("evidence", default=[]),
                estimated_beneficiaries=pick("estimated_beneficiaries", default=0),
                age_groups=pick("age_groups"), urgency=pick("urgency", default=3),
                elderly_covered=pick("elderly_covered"),
                child_covered=pick("child_covered"), at=ts)
        if command_type == "merge_demands":
            return self.merge_demands(
                pick("parent_id"), pick("child_id"),
                pick("merged_by", default=actor_id), pick("reason", default=""),
                at=ts)
        if command_type == "open_cycle":
            return self.open_cycle(
                pick("cycle_id"), pick("year"),
                total_budget=pick("total_budget", default=0),
                year1_budget=pick("year1_budget"),
                year2_budget=pick("year2_budget"),
                weights=pick("weights"),
                opened_by=pick("opened_by", default=actor_id), at=ts)
        if command_type == "score_candidates":
            return self.score_candidates(
                pick("cycle_id"), cost_estimates=pick("cost_estimates"), at=ts)
        if command_type == "build_plan":
            return self.build_plan(pick("cycle_id"), at=ts)
        if command_type == "freeze_budget":
            return self.freeze_budget(
                pick("cycle_id"), frozen_by=pick("frozen_by", default=actor_id),
                at=ts)
        if command_type == "confirm_project":
            return self.confirm_project(
                pick("project_id"), pick("level"), pick("approver_id"), at=ts)
        if command_type == "change_approver":
            return self.change_approver(
                pick("project_id"), pick("level"), pick("new_approver_id"),
                pick("reason", default=""),
                changed_by=pick("changed_by", default=actor_id), at=ts)
        if command_type == "withdraw_project":
            return self.withdraw_project(
                pick("project_id"), pick("reason", default=""),
                pick("notified_communities", default=[]),
                actor_id=actor_id, at=ts)
        if command_type == "delay_project":
            return self.delay_project(
                pick("project_id"), pick("to_year"), pick("reason", default=""),
                pick("notified_communities", default=[]),
                actor_id=actor_id, at=ts)
        raise ValueError(f"不支持的命令类型：{command_type}")

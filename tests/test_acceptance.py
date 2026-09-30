"""验收场景测试。

覆盖题目要求的全部可重放场景：
1. 重复提交与社区合并（证据必填、命令幂等、覆盖人口累加）；
2. 可解释评分（人口覆盖/紧急程度/老幼权重、评分卡逐项可核对）；
3. 跨年度排序（两年计划、预算超支递延、已补齐诉求次年不再入候选）；
4. 两级确认与冻结保护（冻结后不得改分，篡改可被发现）；
5. 审批人变更（只换责任人、绝不改分）；
6. 撤回/延期必须说明影响并通知全部相关社区；
7. 每个决定可查询输入快照与当前推进状态；
8. 命令日志重放后哈希链、快照编号与最终状态完全一致。
"""
import json
import unittest

from community_needs.api import handle
from community_needs.service import Service
from community_needs.store import Store


class 固定时钟:
    """每个命令一个确定时间戳，保证重放结果逐字节可比。"""

    def __init__(self):
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-01-{self.n:02d}T08:00:00+00:00"


EVIDENCE = [{"evidence_id": "ev-1", "kind": "survey",
             "description": "居民联名问卷与现场照片", "uri": "file://evidence/1"}]


def 新服务():
    service = Service(Store())
    service.clock = 固定时钟()
    return service


def 建世界(service: Service, y1=100, y2=100, costs=None):
    """三个社区的典型诉求：d1/d2 同类（养老助餐，多社区重复），d3 菜市场。"""
    costs = costs or {"养老助餐点": 80, "菜市场": 250}
    service.dispatch("submit_demand", "rep-1", {
        "demand_id": "d1", "community_id": "c1", "submitter_id": "rep-1",
        "facility_type": "养老助餐点", "description": "老人吃饭难",
        "evidence": EVIDENCE, "estimated_beneficiaries": 300,
        "age_groups": ["elderly"], "urgency": 5}, command_id="cmd-d1")
    d2 = service.dispatch("submit_demand", "rep-2", {
        "demand_id": "d2", "community_id": "c2", "submitter_id": "rep-2",
        "facility_type": "养老助餐点", "description": "本社区也缺助餐",
        "evidence": EVIDENCE, "estimated_beneficiaries": 200,
        "age_groups": ["elderly"], "urgency": 4}, command_id="cmd-d2")
    service.dispatch("submit_demand", "rep-3", {
        "demand_id": "d3", "community_id": "c3", "submitter_id": "rep-3",
        "facility_type": "菜市场", "description": "买菜要走两公里",
        "evidence": EVIDENCE, "estimated_beneficiaries": 800,
        "age_groups": ["adult"], "urgency": 3}, command_id="cmd-d3")
    service.dispatch("merge_demands", "community-c1", {
        "parent_id": "d1", "child_id": "d2",
        "reason": "同业态重复诉求，跨社区合并"}, command_id="cmd-merge")
    service.dispatch("open_cycle", "street-1", {
        "cycle_id": "cy2026", "year": 2026,
        "year1_budget": y1, "year2_budget": y2}, command_id="cmd-cycle")
    service.dispatch("score_candidates", "street-1", {
        "cycle_id": "cy2026", "cost_estimates": costs}, command_id="cmd-score")
    plan = service.dispatch("build_plan", "street-1", {
        "cycle_id": "cy2026"}, command_id="cmd-plan")
    return d2, plan


class 重复提交与合并测试(unittest.TestCase):
    def setUp(self):
        self.service = 新服务()

    def test_无证据诉求被拒绝(self):
        with self.assertRaisesRegex(ValueError, "证据"):
            self.service.dispatch("submit_demand", "rep-x", {
                "demand_id": "dx", "community_id": "c1",
                "submitter_id": "rep-x", "facility_type": "修鞋点",
                "description": "无证据", "evidence": []})

    def test_相同命令编号重放为幂等(self):
        payload = {
            "demand_id": "d1", "community_id": "c1", "submitter_id": "rep-1",
            "facility_type": "养老助餐点", "description": "老人吃饭难",
            "evidence": EVIDENCE, "estimated_beneficiaries": 300,
            "age_groups": ["elderly"], "urgency": 5}
        first = self.service.dispatch("submit_demand", "rep-1", payload,
                                      command_id="cmd-d1")
        second = self.service.dispatch("submit_demand", "rep-1", dict(payload),
                                       command_id="cmd-d1")
        self.assertEqual(first, second)
        self.assertEqual(len(self.service.store.iter_events()), 1)

    def test_重复诉求被标记且不覆盖原始诉求(self):
        建世界(self.service)
        again = self.service.dispatch("submit_demand", "rep-1", {
            "demand_id": "d1", "community_id": "c1", "submitter_id": "rep-1",
            "facility_type": "x", "description": "y",
            "evidence": EVIDENCE}, command_id="cmd-d1-again")
        self.assertTrue(again["deduplicated"])
        demand = self.service.get_demand("d1")
        self.assertEqual(demand["facility_type"], "养老助餐点")
        self.assertEqual(demand["description"], "老人吃饭难")

    def test_提交时提示相似诉求_合并后人口累加且跨社区(self):
        d2, _ = 建世界(self.service)
        self.assertEqual(d2["similar_demands"], ["d1"])
        merged = self.service.get_demand("d1")
        self.assertEqual(merged["group_demand_ids"], ["d1", "d2"])
        self.assertEqual(merged["group_population_covered"], 500)
        d2_view = self.service.get_demand("d2")
        self.assertEqual(d2_view["merged_into"], "d1")

    def test_不同业态不得合并(self):
        建世界(self.service)
        with self.assertRaisesRegex(ValueError, "同类业态"):
            self.service.dispatch("merge_demands", "community", {
                "parent_id": "d1", "child_id": "d3"})


class 评分与两年计划测试(unittest.TestCase):
    def setUp(self):
        self.service = 新服务()
        _, self.plan = 建世界(self.service)

    def test_老幼权重使覆盖较小但服务老幼的诉求排前(self):
        candidates = self.service.list_candidates("cy2026")
        ranking = [(c["source_demand_id"], c["total_score"]) for c in candidates]
        self.assertEqual(ranking[0][0], "d1")
        top = candidates[0]["score_card"]
        self.assertEqual(top["formula"],
                         "0.5*人口覆盖 + 0.3*紧急程度 + 0.2*老幼服务权重")
        comp = top["components"]
        # 300+200 / 800(最大覆盖组) = 62.5；紧急 5/5 = 100；老幼占比 100
        self.assertEqual(comp["population_coverage"]["weighted_0_100"], 62.5)
        self.assertEqual(comp["urgency"]["weighted_0_100"], 100.0)
        self.assertEqual(comp["elderly_child_service"]["weighted_0_100"], 100.0)
        self.assertEqual(top["total_score"], 81.25)
        # 菜市场人口满分但无老幼权重，总分更低
        self.assertEqual(ranking[1], ("d3", 68.0))

    def test_预算超支项目显式递延且不超支(self):
        self.assertEqual(self.plan["year1_spend"], 80)
        self.assertEqual(self.plan["year2_spend"], 0)
        deferred = self.plan["deferred"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["facility_type"], "菜市场")
        self.assertIn("递延", deferred[0]["reason"])
        self.assertEqual([p["facility_type"] for p in self.plan["planned"]],
                         ["养老助餐点"])

    def test_第二年预算可容纳时排入次年(self):
        service = 新服务()
        _, plan = 建世界(service, y1=100, y2=300)
        years = {p["facility_type"]: p["plan_year"] for p in plan["planned"]}
        self.assertEqual(years["养老助餐点"], 2026)
        self.assertEqual(years["菜市场"], 2027)
        self.assertEqual(plan["deferred"], [])

    def test_跨年度排序_已补齐诉求次年不再入候选_递延项次年入计划(self):
        # 完成 2026 周期：助餐点两级确认并冻结
        for p in self.plan["planned"]:
            self.service.confirm_project(p["project_id"], "street", "zhang")
            self.service.confirm_project(p["project_id"], "merchant", "boss-li")
        self.service.freeze_budget("cy2026", frozen_by="director")
        # 2027 新周期，预算充足
        self.service.dispatch("open_cycle", "street-1", {
            "cycle_id": "cy2027", "year": 2027,
            "year1_budget": 500, "year2_budget": 500}, command_id="cmd-cycle-27")
        self.service.dispatch("score_candidates", "street-1", {
            "cycle_id": "cy2027",
            "cost_estimates": {"养老助餐点": 80, "菜市场": 250}},
            command_id="cmd-score-27")
        candidates_27 = self.service.list_candidates("cy2027")
        self.assertEqual([c["source_demand_id"] for c in candidates_27], ["d3"])
        plan27 = self.service.dispatch("build_plan", "street-1", {
            "cycle_id": "cy2027"}, command_id="cmd-plan-27")
        self.assertEqual([p["facility_type"] for p in plan27["planned"]],
                         ["菜市场"])
        self.assertEqual(plan27["planned"][0]["plan_year"], 2027)


class 确认冻结与审批人变更测试(unittest.TestCase):
    def setUp(self):
        self.service = 新服务()
        _, plan = 建世界(self.service)
        self.pid = plan["planned"][0]["project_id"]

    def test_必须先街道后商户(self):
        with self.assertRaisesRegex(ValueError, "街道级确认"):
            self.service.confirm_project(self.pid, "merchant", "boss-li")
        self.service.confirm_project(self.pid, "street", "zhang")
        self.service.confirm_project(self.pid, "merchant", "boss-li")
        proj = self.service.get_project(self.pid)
        self.assertTrue(proj["street_confirmed"])
        self.assertTrue(proj["merchant_confirmed"])
        self.assertEqual(proj["status"], "active")

    def test_两级确认前不得冻结(self):
        with self.assertRaisesRegex(ValueError, "两级确认"):
            self.service.freeze_budget("cy2026")
        self.service.confirm_project(self.pid, "street", "zhang")
        with self.assertRaisesRegex(ValueError, "两级确认"):
            self.service.freeze_budget("cy2026")

    def test_冻结后不得改分或重建计划(self):
        self.service.confirm_project(self.pid, "street", "zhang")
        self.service.confirm_project(self.pid, "merchant", "boss-li")
        self.service.freeze_budget("cy2026", frozen_by="director")
        with self.assertRaisesRegex(ValueError, "冻结"):
            self.service.score_candidates("cy2026",
                                          cost_estimates={"养老助餐点": 1})
        with self.assertRaisesRegex(ValueError, "冻结"):
            self.service.build_plan("cy2026")

    def test_审批人变更只换责任人不改分(self):
        self.service.confirm_project(self.pid, "street", "zhang")
        before = self.service.get_project(self.pid)
        with self.assertRaisesRegex(ValueError, "说明原因"):
            self.service.change_approver(self.pid, "street", "zhao", "")
        change = self.service.change_approver(
            self.pid, "street", "zhao", "原负责人轮岗", changed_by="director")
        after = self.service.get_project(self.pid)
        self.assertEqual(after["street_confirmed_by"], "zhao")
        self.assertEqual(after["score"], before["score"])
        self.assertEqual(change["score_snapshot_id"], after["snapshot_id"])
        decision = self.service.get_decision(self.pid)
        trail_types = [(t["event_type"], t["payload"]["new_approver_id"])
                       for t in decision["decision_trail"]
                       if t["event_type"] == "approver_changed"]
        self.assertEqual(trail_types, [("approver_changed", "zhao")])
        # 商户层级尚未产生审批人，不能“变更”（应直接确认）
        with self.assertRaisesRegex(ValueError, "尚未产生审批人"):
            self.service.change_approver(self.pid, "merchant", "wang", "换人")
        self.service.confirm_project(self.pid, "merchant", "boss-li")

    def test_篡改历史事件或快照会被完整性校验发现(self):
        self.assertTrue(self.service.verify_integrity()["chain_ok"])
        conn = self.service.store.connection
        conn.execute("UPDATE event_log SET payload=? WHERE seq=1",
                     (json.dumps({"tampered": True}, ensure_ascii=False),))
        conn.commit()
        self.assertFalse(self.service.verify_integrity()["chain_ok"])


class 撤回延期与通知测试(unittest.TestCase):
    def setUp(self):
        self.service = 新服务()
        _, plan = 建世界(self.service, y1=100, y2=300)
        self.plan = plan
        self.p_助餐 = plan["planned"][0]["project_id"]
        self.p_菜场 = plan["planned"][1]["project_id"]

    def test_撤回必须说明影响并通知全部相关社区(self):
        with self.assertRaisesRegex(ValueError, "原因与影响"):
            self.service.withdraw_project(self.p_助餐, "", ["c1", "c2"])
        with self.assertRaisesRegex(ValueError, "c2"):
            self.service.withdraw_project(self.p_助餐, "商户无法进驻", ["c1"])
        result = self.service.withdraw_project(
            self.p_助餐, "商户无法进驻，年内无替代点位", ["c1", "c2"])
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["impact"]["affected_communities"], ["c1", "c2"])
        self.assertEqual(result["impact"]["coverage_lost"], 500)
        self.assertEqual(result["impact"]["budget_freed"], 80)
        self.assertEqual(result["score_snapshot_id"],
                         self.service.get_project(self.p_助餐)["snapshot_id"])
        # 空出的预算给出按原分数递补建议
        self.assertEqual(result["promotable_deferred"], [])

    def test_社区可查询自己收到的影响通知(self):
        self.service.withdraw_project(
            self.p_助餐, "商户无法进驻", ["c1", "c2"], actor_id="street-1")
        notes_c1 = self.service.list_notifications("c1")
        notes_c3 = self.service.list_notifications("c3")
        self.assertEqual(len(notes_c1), 1)
        self.assertEqual(notes_c1[0]["impact"]["coverage_lost"], 500)
        self.assertEqual(notes_c3, [])
        proj = self.service.get_project(self.p_助餐)
        self.assertEqual(proj["notified_communities"], ["c1", "c2"])
        self.assertEqual(proj["status_reason"], "商户无法进驻")

    def test_延期更新计划年度并保留通知记录(self):
        # 菜市场排在 2027（第二年）
        self.assertEqual(self.service.get_project(self.p_菜场)["plan_year"], 2027)
        result = self.service.delay_project(
            self.p_菜场, 2028, "施工招标流标，顺延一年", ["c3"])
        self.assertEqual(result["status"], "delayed")
        proj = self.service.get_project(self.p_菜场)
        self.assertEqual(proj["plan_year"], 2028)
        self.assertEqual(proj["notified_communities"], ["c3"])
        with self.assertRaisesRegex(ValueError, "晚于当前计划年度"):
            self.service.delay_project(
                self.p_菜场, 2028, "再次延期", ["c3"])

    def test_冻结后撤回仍然留痕且分数不变(self):
        for pid in (self.p_助餐, self.p_菜场):
            self.service.confirm_project(pid, "street", "zhang")
            self.service.confirm_project(pid, "merchant", "boss-li")
        self.service.freeze_budget("cy2026", frozen_by="director")
        score_before = self.service.get_project(self.p_助餐)["score"]
        self.service.withdraw_project(self.p_助餐, "冻结后商户退出", ["c1", "c2"])
        score_after = self.service.get_project(self.p_助餐)["score"]
        self.assertEqual(score_before, score_after)
        with self.assertRaisesRegex(ValueError, "冻结"):
            self.service.score_candidates("cy2026")


class 决定溯源测试(unittest.TestCase):
    def setUp(self):
        self.service = 新服务()
        _, plan = 建世界(self.service)
        self.pid = plan["planned"][0]["project_id"]

    def test_查询显示输入快照与当前推进状态(self):
        self.service.confirm_project(self.pid, "street", "zhang")
        decisions = self.service.list_decisions("cy2026")
        d = next(x for x in decisions if x["project_id"] == self.pid)
        self.assertTrue(d["score_snapshot_id"].startswith("snap_scoring_"))
        self.assertTrue(d["plan_snapshot_id"].startswith("snap_plan_"))
        self.assertEqual(d["status"], "proposed")
        self.assertEqual(d["street_confirmed_by"], "zhang")
        detail = self.service.get_decision(self.pid)
        # 评分卡可解释
        self.assertEqual(detail["score_card"]["total_score"], 81.25)
        # 评分输入快照包含全部诉求（含被合并的 d2）、权重与预算
        snap = detail["score_snapshot"]
        self.assertEqual(snap["weights"],
                         {"population": 0.5, "urgency": 0.3, "vulnerable": 0.2})
        roots = {g["root_demand"]["demand_id"] for g in snap["groups"]}
        self.assertEqual(roots, {"d1", "d3"})
        d1_group = next(g for g in snap["groups"]
                        if g["root_demand"]["demand_id"] == "d1")
        self.assertEqual(d1_group["population_covered"], 500)
        self.assertEqual(d1_group["communities"], ["c1", "c2"])
        # 计划快照记录了排序与递延
        self.assertEqual(len(detail["plan_snapshot"]["items"]), 2)

    def test_快照编号可由内容重新算出(self):
        from community_needs.events import new_snapshot_id
        detail = self.service.get_decision(self.pid)
        self.assertEqual(
            new_snapshot_id("scoring", detail["score_snapshot"]),
            detail["score_snapshot_id"])
        self.assertEqual(
            new_snapshot_id("plan", detail["plan_snapshot"]),
            detail["plan_snapshot_id"])


class 命令重放与接口测试(unittest.TestCase):
    def test_完整流程重放后哈希链快照与状态一致(self):
        service = 新服务()
        _, plan = 建世界(service, y1=100, y2=300)
        pid = plan["planned"][0]["project_id"]
        pid2 = plan["planned"][1]["project_id"]
        # 全部变更走统一命令分发，才能进入命令日志被重放
        service.dispatch("confirm_project", "zhang",
                         {"project_id": pid, "level": "street",
                          "approver_id": "zhang"}, command_id="cmd-c1s")
        service.dispatch("change_approver", "director",
                         {"project_id": pid, "level": "street",
                          "new_approver_id": "zhao", "reason": "轮岗"},
                         command_id="cmd-ch1")
        service.dispatch("confirm_project", "boss-li",
                         {"project_id": pid, "level": "merchant",
                          "approver_id": "boss-li"}, command_id="cmd-c1m")
        service.dispatch("confirm_project", "zhang",
                         {"project_id": pid2, "level": "street",
                          "approver_id": "zhang"}, command_id="cmd-c2s")
        service.dispatch("confirm_project", "boss-wang",
                         {"project_id": pid2, "level": "merchant",
                          "approver_id": "boss-wang"}, command_id="cmd-c2m")
        service.dispatch("freeze_budget", "director",
                         {"cycle_id": "cy2026"}, command_id="cmd-freeze")
        service.dispatch("withdraw_project", "street-1",
                         {"project_id": pid, "reason": "商户退出",
                          "notified_communities": ["c1", "c2"]},
                         command_id="cmd-wd")
        service.dispatch("delay_project", "street-1",
                         {"project_id": pid2, "to_year": 2028, "reason": "流标",
                          "notified_communities": ["c3"]},
                         command_id="cmd-dl")

        report = service.replay_command_log()
        self.assertTrue(report["event_hashes_match"])
        self.assertTrue(report["snapshots_match"])
        self.assertTrue(report["projection_match"])
        self.assertGreater(report["commands_replayed"], 0)
        self.assertEqual(report["original_event_count"],
                         report["replayed_event_count"])
        self.assertTrue(service.verify_integrity()["chain_ok"])

    def test_json适配层端到端(self):
        svc = Service(Store())
        svc.clock = 固定时钟()

        def call(action, actor="", **fields):
            payload = {"action": action, "actor_id": actor, **fields}
            return json.loads(handle(json.dumps(payload, ensure_ascii=False), svc))

        self.assertEqual(call("health")["status"], "ok")
        submitted = call(
            "submit_demand", "rep-1", command_id="j-1",
            demand_id="j1", community_id="c1", submitter_id="rep-1",
            facility_type="养老助餐点", description="缺", evidence=EVIDENCE,
            estimated_beneficiaries=100, age_groups=["elderly"], urgency=4)
        self.assertEqual(submitted["state"], "submitted")
        call("open_cycle", "street", command_id="j-2", cycle_id="cyj",
             year=2026, year1_budget=500, year2_budget=500)
        call("score_candidates", "street", command_id="j-3", cycle_id="cyj",
             cost_estimates={"养老助餐点": 120})
        call("build_plan", "street", command_id="j-4", cycle_id="cyj")
        decisions = call("list_decisions", cycle_id="cyj")
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]["score_snapshot_id"])


if __name__ == "__main__":
    unittest.main()

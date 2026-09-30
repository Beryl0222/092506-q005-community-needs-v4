"""居民需求与推进计划的基础领域对象。

所有对象均为不可变值对象。会随时间变化的“当前状态”不在这些对象上
原地修改，而是由 ``service`` 层基于追加事件重放得到；每个决定都保存
其输入快照（见 ``events.py``），因此历史决定可解释、可重放。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_iso() -> str:
    """统一的 UTC 时间戳，便于跨年度排序与重放比对。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 采集阶段
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """居民代表提交缺口时附带的证据。"""
    evidence_id: str
    kind: str            # photo / survey / receipt / document / other
    description: str
    uri: str = ""


@dataclass(frozen=True)
class Demand:
    """一条居民诉求（带证据的缺口）。

    - ``merged_into`` 非空表示该诉求已被合并进另一个诉求，自身不再独立计分。
    - ``supporting_demands`` 是被合并进来的诉求编号，扩大人口覆盖。
    """
    demand_id: str
    community_id: str
    submitter_id: str
    facility_type: str          # 如 菜市场 / 养老助餐点 / 托育点
    description: str
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)
    estimated_beneficiaries: int = 0
    age_groups: tuple[str, ...] = field(default_factory=tuple)  # elderly / child / adult
    urgency: int = 3            # 1-5，越大越紧急
    created_at: str = ""
    merged_into: str = ""
    supporting_demands: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 决策阶段
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreCard:
    """可解释评分卡（0-100）。

    总分 = round(0.5*人口覆盖 + 0.3*紧急程度 + 0.2*老幼服务权重)
    """
    population_component: float
    urgency_component: float
    vulnerable_component: float
    total_score: float
    population_covered: int
    elderly_covered: int
    child_covered: int
    weight_population: float = 0.5
    weight_urgency: float = 0.3
    weight_vulnerable: float = 0.2

    def breakdown(self) -> dict:
        return {
            "formula": "0.5*人口覆盖 + 0.3*紧急程度 + 0.2*老幼服务权重",
            "weights": {
                "population": self.weight_population,
                "urgency": self.weight_urgency,
                "vulnerable": self.weight_vulnerable,
            },
            "components": {
                "population_coverage": {
                    "raw_covered": self.population_covered,
                    "weighted_0_100": round(self.population_component, 2),
                },
                "urgency": {
                    "weighted_0_100": round(self.urgency_component, 2),
                },
                "elderly_child_service": {
                    "elderly_covered": self.elderly_covered,
                    "child_covered": self.child_covered,
                    "weighted_0_100": round(self.vulnerable_component, 2),
                },
            },
            "total_score": self.total_score,
        }


@dataclass(frozen=True)
class Candidate:
    """候选项目：一次年度评分运行对一个合并诉求的评分结果。"""
    candidate_id: str
    cycle_id: str
    source_demand_id: str
    communities: tuple[str, ...]
    facility_type: str
    score_card: ScoreCard
    estimated_cost: int
    population_covered: int
    snapshot_id: str          # 评分时使用的输入快照
    scored_at: str


@dataclass(frozen=True)
class Project:
    """进入两年推进计划的项目及其审批与推进状态。"""
    project_id: str
    cycle_id: str
    candidate_id: str
    demand_id: str
    facility_type: str
    communities: tuple[str, ...]
    score: float
    estimated_cost: int
    plan_year: int             # 1 或 2
    rank: int
    street_confirmed: bool = False
    street_confirmed_by: str = ""
    merchant_confirmed: bool = False
    merchant_confirmed_by: str = ""
    status: str = "proposed"   # proposed/active/withdrawn/delayed/completed
    status_reason: str = ""
    affected_communities: tuple[str, ...] = field(default_factory=tuple)
    notified_communities: tuple[str, ...] = field(default_factory=tuple)
    snapshot_id: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Budget:
    """年度周期预算。frozen 后评分与计划进入不可变状态。"""
    cycle_id: str
    year: int
    total_budget: int
    frozen: bool = False
    frozen_at: str = ""


# ---------------------------------------------------------------------------
# 基线兼容
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Record:
    """最早版本保留的通用登记记录。"""
    record_id: str
    owner_id: str
    state: str = "draft"
    created_at: str = ""

    def with_timestamp(self) -> "Record":
        return Record(self.record_id, self.owner_id, self.state,
                      self.created_at or now_iso())

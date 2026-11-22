"""合同调价条款的执行与复核。

处理要点：

* 调价区间以合同窗口首月与末月的**同一序列**观测值为两端；主序列任一端
  未公示时，只有合同登记了 fallback、且 fallback 在**两端都公示**时才整体
  转用备选序列，绝不把两条序列的基期混接。
* 调价比例按合同 max_change 双向限幅，金额以分向下取整（四舍五入到分）。
* 已审签合同以 issued_prices 记录的当时观测值与金额为准；后续复核只做
  对照，不因候选指标更全面而自动重开。
* late_rule 只在确有公示值更正记录时决定是否形成差额。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from .index import BASELINE, Policy, SeriesResult, compile_series, with_bridge_state
from .model import Bundle, Contract

CENT = Decimal("1")


# ---------------------------------------------------------------------------
# 情景
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """一次用于敏感性或修订对照的编制情景。

    ``kind`` 决定它的用途：
    * ``baseline``：今天即可采用的正式口径，只有它进入当前审签区间；
    * ``contingent``：在完成指定的补证或衔接审定之后才可能采用的 prospectively 情景，
      只说明“补证后能到哪一步”，不能当作今天已可审签；
    * ``sensitivity``：口径压力测试，任何情况下都不进入审签。
    """

    name: str
    bundle: Bundle
    policy: Policy
    description: str = ""
    kind: str = "contingent"

    def __post_init__(self) -> None:
        if self.kind not in {"baseline", "contingent", "sensitivity"}:
            raise ValueError(f"未知情景类别：{self.kind}")

    @property
    def admissible(self) -> bool:
        """基线或补证后可成立的情景构成审签区间；纯口径敏感度不进入。"""
        return self.kind in {"baseline", "contingent"}


def _replace_bridge(bundle: Bundle, bridge_id: str, **changes) -> Bundle:
    bridges = [
        replace(bridge, **changes) if bridge.id == bridge_id else bridge
        for bridge in bundle.bridges
    ]
    return replace(bundle, bridges=bridges)


def standard_scenarios(bundle: Bundle) -> list[Scenario]:
    """报告使用的固定情景；名称在输出中保持稳定，便于回归比较。"""
    scenarios = [
        Scenario(
            "baseline",
            bundle,
            BASELINE,
            "基线：accepted 衔接、支出权数、缺报剔除",
            kind="baseline",
        ),
        Scenario(
            "missing_carry",
            bundle,
            replace(BASELINE, missing="carry"),
            "缺报沿用最近可得价格（敏感性口径，不作公示、不进入审签区间）",
            kind="sensitivity",
        ),
        Scenario(
            "quantity_weight",
            bundle,
            replace(BASELINE, weight="quantity"),
            "以基期交易量代替基期支出作权数（口径敏感度）",
            kind="sensitivity",
        ),
        Scenario(
            "package_weighted",
            bundle,
            replace(BASELINE, package_policy="package"),
            "同一批次保留整包、剔除拆分规格（口径敏感度）",
            kind="sensitivity",
        ),
        Scenario(
            "accept_filed_with_evidence",
            with_bridge_state(bundle, {"B4"}, "accepted"),
            BASELINE,
            "把有重叠证据支持的 filed 衔接 B4 审定为 accepted 后再编制",
            kind="contingent",
        ),
        Scenario(
            "reinstate_B2",
            with_bridge_state(bundle, {"B2"}, "accepted"),
            BASELINE,
            "恢复被撤回的 B2（重叠比价 1.2538 与登记 1.28 相符）",
            kind="contingent",
        ),
        Scenario(
            "renew_B2_through_2026_08",
            _replace_bridge(
                with_bridge_state(bundle, {"B2"}, "accepted"),
                "B2",
                last_period="2026-08",
            ),
            BASELINE,
            "B2 恢复并续登至 2026-08，使 west 的 bath_one 可由 bath_travel 换算",
            kind="contingent",
        ),
        Scenario(
            "coverage_70pct",
            bundle,
            replace(BASELINE, min_coverage_override=Decimal("0.70")),
            "覆盖率门槛降至 70%（只说明断点对门槛的敏感程度，不改变合同约定）",
            kind="sensitivity",
        ),
        Scenario(
            "no_bridges",
            bundle,
            replace(BASELINE, bridge_states=frozenset()),
            "完全不使用任何衔接（说明缺少可比价格时指数会怎样中断）",
            kind="sensitivity",
        ),
    ]
    return scenarios


# ---------------------------------------------------------------------------
# 合同求值
# ---------------------------------------------------------------------------


@dataclass
class Endpoint:
    period: str
    series: str
    index: Decimal | None
    published: bool
    coverage: Decimal
    status: str
    missing: list[dict] = field(default_factory=list)

    def describe(self) -> str:
        if self.published and self.index is not None:
            return f"{self.series} {self.period} = {self.index}"
        return f"{self.series} {self.period} 未公示（覆盖 {self.coverage}）"

    @property
    def available(self) -> bool:
        return self.published and self.index is not None


@dataclass
class ContractEvaluation:
    contract: Contract
    scenario: str
    signable: bool
    series_used: str | None
    fallback_used: bool
    base: Endpoint | None
    end: Endpoint | None
    ratio_raw: Decimal | None
    ratio_applied: Decimal | None
    amount: Decimal | None
    upper_ratio: Decimal
    lower_ratio: Decimal
    cap_binding: str  # none | upper | lower
    blockers: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    issued: bool = False

    @property
    def change(self) -> Decimal | None:
        if self.ratio_applied is None:
            return None
        return self.ratio_applied - 1


def _endpoint(catalog: dict[str, SeriesResult], series: str, period: str) -> Endpoint:
    result = catalog.get(series)
    if result is None:
        return Endpoint(period, series, None, False, Decimal(0), "系列未编制")
    period_result = result.periods.get(period)
    if period_result is None:
        return Endpoint(period, series, None, False, Decimal(0), "不在编制区间")
    missing = [
        {
            "area": trace.cell.area,
            "supplier": trace.cell.supplier,
            "spec": trace.cell.spec,
            "base_id": trace.cell.base_id,
            "status": trace.resolution.status,
            "detail": trace.resolution.detail,
            "weight": trace.weight,
        }
        for trace in period_result.missing
    ]
    return Endpoint(
        period=period,
        series=series,
        index=period_result.published_index,
        published=period_result.published,
        coverage=period_result.coverage,
        status="已公示" if period_result.published else "未达最低覆盖",
        missing=missing,
    )


def _required_evidence(endpoint: Endpoint, limit: int = 6) -> list[str]:
    items: list[str] = []
    for item in endpoint.missing[:limit]:
        items.append(
            f"{item['area']}/{item['supplier']}/{item['spec']}：{item['detail']}"
        )
    return items


def evaluate_contract(
    bundle: Bundle,
    contract: Contract,
    catalog: dict[str, SeriesResult],
    scenario: str = "baseline",
) -> ContractEvaluation:
    primary_base = _endpoint(catalog, contract.series, contract.first_period)
    primary_end = _endpoint(catalog, contract.series, contract.last_period)
    upper = Decimal(1) + contract.max_change
    lower = Decimal(1) - contract.max_change

    chosen: tuple[Endpoint, Endpoint, str, bool] | None = None
    blockers: list[dict] = []
    notes: list[str] = []

    if primary_base.available and primary_end.available:
        chosen = (primary_base, primary_end, contract.series, False)
    elif contract.fallback:
        fallback_base = _endpoint(catalog, contract.fallback, contract.first_period)
        fallback_end = _endpoint(catalog, contract.fallback, contract.last_period)
        if fallback_base.available and fallback_end.available:
            chosen = (
                fallback_base,
                fallback_end,
                contract.fallback,
                True,
            )
            notes.append(
                "主序列至少一端未公示，按合同 fallback 条款整体转用 "
                f"{contract.fallback}；两端均取自该序列，不混接基期。"
            )
            for endpoint in (primary_base, primary_end):
                if not endpoint.available:
                    notes.append(f"主序列未直接采用：{endpoint.describe()}")
        else:
            blockers.append(
                {
                    "code": "primary_and_fallback_incomplete",
                    "detail": (
                        f"主序列 {contract.series} 端点不完整，"
                        f"备选 {contract.fallback} 也未能在两端同时公示"
                    ),
                    "primary": [primary_base.describe(), primary_end.describe()],
                    "fallback": [fallback_base.describe(), fallback_end.describe()],
                }
            )
    else:
        blockers.append(
            {
                "code": "no_fallback",
                "detail": (
                    f"主序列 {contract.series} 端点未公示，合同未登记可用备选序列，"
                    "须在原口径下等待补证"
                ),
                "primary": [primary_base.describe(), primary_end.describe()],
            }
        )

    if chosen is None:
        base_endpoint, end_endpoint = primary_base, primary_end
        required = []
        for endpoint in (primary_base, primary_end):
            if not endpoint.available:
                required.extend(_required_evidence(endpoint))
        if required:
            blockers[-1]["required_evidence"] = required
        return ContractEvaluation(
            contract=contract,
            scenario=scenario,
            signable=False,
            series_used=None,
            fallback_used=False,
            base=base_endpoint,
            end=end_endpoint,
            ratio_raw=None,
            ratio_applied=None,
            amount=None,
            upper_ratio=upper,
            lower_ratio=lower,
            cap_binding="none",
            blockers=blockers,
            notes=notes,
            issued=contract.state == "issued",
        )

    base_endpoint, end_endpoint, series_used, fallback_used = chosen
    ratio_raw = end_endpoint.index / base_endpoint.index
    if ratio_raw > upper:
        ratio_applied = upper
        cap_binding = "upper"
    elif ratio_raw < lower:
        ratio_applied = lower
        cap_binding = "lower"
    else:
        ratio_applied = ratio_raw
        cap_binding = "none"
    amount = (contract.reference_price * ratio_applied).quantize(CENT, rounding=ROUND_HALF_UP)

    if contract.first_period > contract.last_period:
        raise ValueError(f"合同 {contract.contract} 的窗口无效")
    if cap_binding != "none":
        notes.append(
            f"未限幅比例 {ratio_raw:.6f}，按 max_change={contract.max_change} "
            f"限幅为 {ratio_applied:.6f}"
        )
    return ContractEvaluation(
        contract=contract,
        scenario=scenario,
        signable=True,
        series_used=series_used,
        fallback_used=fallback_used,
        base=base_endpoint,
        end=end_endpoint,
        ratio_raw=ratio_raw,
        ratio_applied=ratio_applied,
        amount=amount,
        upper_ratio=upper,
        lower_ratio=lower,
        cap_binding=cap_binding,
        blockers=blockers,
        notes=notes,
        issued=contract.state == "issued",
    )


# ---------------------------------------------------------------------------
# 情景目录与区间
# ---------------------------------------------------------------------------


def compile_catalog(
    bundle: Bundle, policy: Policy = BASELINE, series: Iterable[str] | None = None
) -> dict[str, SeriesResult]:
    names = list(series) if series is not None else list(bundle.series)
    return {name: compile_series(bundle, name, policy) for name in names}


def scenario_catalogs(scenarios: list[Scenario]) -> dict[str, dict[str, SeriesResult]]:
    return {scenario.name: compile_catalog(scenario.bundle, scenario.policy) for scenario in scenarios}


@dataclass
class ContractReview:
    contract: Contract
    baseline: ContractEvaluation
    outcomes: list[ContractEvaluation]
    admissible_names: frozenset[str] = frozenset()
    contingent_names: frozenset[str] = frozenset()

    def _admissible(self) -> list[ContractEvaluation]:
        return [
            item
            for item in self.outcomes
            if item.scenario in self.admissible_names
        ]

    @property
    def signable_outcomes(self) -> list[ContractEvaluation]:
        """今天即可审签的结果；不含补证后才会成立的情景。"""
        return [item for item in self._admissible() if item.signable]

    @property
    def contingent_outcomes(self) -> list[ContractEvaluation]:
        return [
            item
            for item in self.outcomes
            if item.scenario in self.contingent_names
        ]

    @property
    def sensitivity_outcomes(self) -> list[ContractEvaluation]:
        return [
            item
            for item in self.outcomes
            if item.scenario not in self.admissible_names
            and item.scenario not in self.contingent_names
        ]

    @property
    def amount_interval(self) -> tuple[Decimal, Decimal] | None:
        amounts = [item.amount for item in self.signable_outcomes if item.amount is not None]
        if not amounts:
            return None
        return min(amounts), max(amounts)

    @property
    def ratio_interval(self) -> tuple[Decimal, Decimal] | None:
        ratios = [item.ratio_applied for item in self.signable_outcomes if item.ratio_applied is not None]
        if not ratios:
            return None
        return min(ratios), max(ratios)

    def interval_within_cap(self) -> bool:
        interval = self.ratio_interval
        if interval is None:
            return False
        return interval[0] >= self.baseline.lower_ratio and interval[1] <= self.baseline.upper_ratio

    def blocking_evidence(self, scenario: str | None = None) -> list[str]:
        seen: set[str] = set()
        items: list[str] = []
        pool = (
            [item for item in [self.baseline, *self.outcomes] if item.scenario == scenario]
            if scenario
            else [self.baseline, *self.outcomes]
        )
        for evaluation in pool:
            for blocker in evaluation.blockers:
                for text in blocker.get("required_evidence", []):
                    if text not in seen:
                        seen.add(text)
                        items.append(text)
        return items


def review_contracts(
    bundle: Bundle,
    contracts: dict[str, Contract] | None = None,
    scenarios: list[Scenario] | None = None,
) -> tuple[dict[str, ContractReview], list[Scenario]]:
    scenarios = scenarios or standard_scenarios(bundle)
    catalogs = scenario_catalogs(scenarios)
    contracts = contracts or bundle.contracts
    admissible = frozenset(s.name for s in scenarios if s.admissible)
    contingent = frozenset(s.name for s in scenarios if s.kind == "contingent")
    reviews: dict[str, ContractReview] = {}
    for name, contract in contracts.items():
        outcomes = [
            evaluate_contract(
                scenario.bundle,
                contract,
                catalogs[scenario.name],
                scenario.name,
            )
            for scenario in scenarios
        ]
        reviews[name] = ContractReview(
            contract=contract,
            baseline=outcomes[0],
            outcomes=outcomes,
            admissible_names=admissible,
            contingent_names=contingent,
        )
    return reviews, scenarios


def splicing_counterexample(
    bundle: Bundle,
    primary: str = "IW",
    fallback: str = "IA",
    period: str = "2026-06",
) -> dict:
    """演示把两条序列首尾相接会把基期差额误当涨价。

    正确的 fallback 做法是在同一合同窗口两端整体采用一条序列；这里仅把
    IW 的末期值与 IA 的同月值并列，比较“混接算出的比价”和“各自序列内的
    比价”，说明混接会把两条序列的基期差额算成涨价或降价，不得用于合同。
    """
    catalog = compile_catalog(bundle, BASELINE)
    primary_result = catalog[primary]
    fallback_result = catalog[fallback]
    primary_periods = [
        p for p in primary_result.periods if primary_result.periods[p].published
    ]
    if not primary_periods:
        return {"ok": False, "reason": f"{primary} 没有已公示月份"}
    last_primary = max(primary_periods)
    spliced = primary_result.periods[last_primary].published_index
    fallback_value = fallback_result.period(period).published_index
    if spliced is None or fallback_value is None:
        return {"ok": False, "reason": "参与对照的月份未公示"}
    naive_ratio = fallback_value / spliced
    fallback_base_period = min(fallback_result.periods)
    fallback_base_value = fallback_result.period(fallback_base_period).published_index
    true_ratio = fallback_value / fallback_base_value if fallback_base_value else None
    return {
        "ok": True,
        "primary": primary,
        "fallback": fallback,
        "last_primary_period": last_primary,
        "primary_value": spliced,
        "period": period,
        "fallback_value": fallback_value,
        "fallback_base_period": fallback_base_period,
        "fallback_base_value": fallback_base_value,
        "naive_spliced_ratio": naive_ratio,
        "true_same_series_ratio": true_ratio,
        "base_difference": (naive_ratio - true_ratio) if true_ratio is not None else None,
    }


# ---------------------------------------------------------------------------
# 已审签结果回查
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Correction:
    """正式登记的公示值更正。没有此类记录时，重算差异不构成重开依据。"""

    series: str
    period: str
    old_value: Decimal
    new_value: Decimal
    recorded_on: str
    reference: str = ""


# 便于 CLI 先按名称引用，再逐步改为语义化别名。
Corrections = Correction


@dataclass
class IssuedReconciliation:
    contract: Contract
    recorded_series: str
    recorded_base: Decimal
    recorded_end: Decimal
    recorded_ratio: Decimal
    recorded_amount: Decimal
    recorded_on: str
    recorded_consistent: bool
    current: ContractEvaluation
    amount_difference: Decimal | None
    ratio_difference: Decimal | None
    published_value_changed: bool
    source_changed: bool
    executable_changed: bool
    corrections: list[Correction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    corrected_amount: Decimal | None = None
    corrected_ratio: Decimal | None = None
    correction_mismatch: bool = False

    @property
    def reopening(self) -> str:
        if not self.recorded_consistent:
            return "记录不自洽：先核查当时采用的指数与条款，不直接重算"
        if self.correction_mismatch:
            return "先核对更正版本：更正原值与当时记录不一致，不直接调整"
        difference = self.amount_difference is not None and self.amount_difference != 0
        if not difference:
            return "不重开：可执行调价未变"
        if self.contract.late_rule == "no_reopen":
            return "不重开：合同约定 no_reopen"
        if not self.published_value_changed:
            return (
                "不重开：重算差异无公示值更正记录支持；"
                "仍以当时采用的指数和合同条款为准"
            )
        if self.executable_changed:
            return "依更正记录按差额处理"
        return "不重开：更正后实际可执行的调价未变"


def _recorded_amount_consistent(contract: Contract, issued) -> bool:
    ratio = issued.end_observed / issued.base_observed
    applied = min(max(ratio, Decimal(1) - contract.max_change), Decimal(1) + contract.max_change)
    expected = (contract.reference_price * applied).quantize(CENT, rounding=ROUND_HALF_UP)
    return expected == issued.amount


def reconcile_issued(
    bundle: Bundle,
    contract: Contract,
    current: ContractEvaluation,
    corrections: list[Correction] | None = None,
) -> IssuedReconciliation | None:
    issued = bundle.issued.get(contract.contract)
    if issued is None:
        return None
    corrections = corrections or []
    recorded_ratio = issued.end_observed / issued.base_observed
    amount_difference = None
    ratio_difference = None
    if current.amount is not None:
        amount_difference = current.amount - issued.amount
    if current.ratio_applied is not None:
        ratio_difference = current.ratio_applied - recorded_ratio

    relevant = [
        item
        for item in corrections
        if item.series == issued.series
        and contract.first_period <= item.period <= contract.last_period
    ]
    published_value_changed = bool(relevant)
    mismatch = [
        item
        for item in relevant
        if (
            item.period == contract.first_period
            and item.old_value != issued.base_observed
        )
        or (
            item.period == contract.last_period
            and item.old_value != issued.end_observed
        )
    ]
    correction_mismatch = bool(mismatch)
    source_changed = current.series_used is not None and current.series_used != issued.series
    recorded_consistent = _recorded_amount_consistent(contract, issued)
    executable_changed = False
    corrected_amount = None
    corrected_ratio = None
    if relevant and recorded_consistent and not correction_mismatch:
        # 用更正后的公示值重算，只有因此改变实际执行的金额才算可执行调价变化。
        base_value = issued.base_observed
        end_value = issued.end_observed
        for item in relevant:
            if item.period == contract.first_period:
                base_value = item.new_value
            if item.period == contract.last_period:
                end_value = item.new_value
        corrected_ratio = end_value / base_value
        corrected_applied = min(
            max(corrected_ratio, Decimal(1) - contract.max_change),
            Decimal(1) + contract.max_change,
        )
        corrected_amount = (contract.reference_price * corrected_applied).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        executable_changed = corrected_amount != issued.amount
        # 回查时金额差以“更正后金额 vs 当时实际支付”表示，而不是本次重算口径。
        amount_difference = corrected_amount - issued.amount
        ratio_difference = corrected_applied - recorded_ratio

    notes: list[str] = []
    if correction_mismatch:
        notes.append(
            "更正记录的“原值”与合同当时记录不一致，须先核对更正对应的版本，"
            "不得直接据以调整已生效单价。"
        )
    if corrected_amount is None and amount_difference is not None and amount_difference != 0:
        notes.append(
            "本次重算与当时记录相差 "
            f"{amount_difference}（重算 {current.amount}，记录 {issued.amount}），"
            "但差异来自口径或后续数据，不是公示值更正，不作为重开依据。"
        )
    if source_changed:
        notes.append(
            f"重算采用的序列为 {current.series_used}，当时记录为 {issued.series}；"
            "回查仍以当时实际采用的序列为准。"
        )
    return IssuedReconciliation(
        contract=contract,
        recorded_series=issued.series,
        recorded_base=issued.base_observed,
        recorded_end=issued.end_observed,
        recorded_ratio=recorded_ratio,
        recorded_amount=issued.amount,
        recorded_on=issued.issued_on,
        recorded_consistent=recorded_consistent,
        current=current,
        amount_difference=amount_difference,
        ratio_difference=ratio_difference,
        published_value_changed=published_value_changed,
        source_changed=source_changed,
        executable_changed=executable_changed,
        corrections=relevant,
        notes=notes,
        corrected_amount=corrected_amount,
        corrected_ratio=corrected_ratio,
        correction_mismatch=correction_mismatch,
    )


# ---------------------------------------------------------------------------
# 修订影响对照
# ---------------------------------------------------------------------------


@dataclass
class SeriesChange:
    series: str
    period: str
    before_value: Decimal | None
    after_value: Decimal | None
    before_published: bool
    after_published: bool
    before_coverage: Decimal
    after_coverage: Decimal

    @property
    def published_value_changed(self) -> bool:
        return (
            self.before_published
            and self.after_published
            and self.before_value != self.after_value
        )

    @property
    def publication_changed(self) -> bool:
        return self.before_published != self.after_published

    @property
    def kind(self) -> str:
        if self.publication_changed:
            return "恢复公示" if self.after_published else "转为中断"
        if self.published_value_changed:
            return "公示值改变"
        return "无变化"


@dataclass
class ContractImpact:
    contract: Contract
    before: ContractEvaluation
    after: ContractEvaluation

    @property
    def source_changed(self) -> bool:
        return self.before.series_used != self.after.series_used

    @property
    def executable_changed(self) -> bool:
        if self.before.amount is None or self.after.amount is None:
            return self.before.signable != self.after.signable
        return self.before.amount != self.after.amount

    @property
    def signability_changed(self) -> bool:
        return self.before.signable != self.after.signable

    @property
    def treatment(self) -> str:
        if not self.executable_changed and not self.source_changed:
            return "无影响"
        if self.before.contract.state == "issued":
            if self.before.contract.late_rule == "no_reopen":
                return "已审签，no_reopen：不重开"
            return "已审签：不自动重开；仅公示值更正可形成差额"
        if not self.before.signable and self.after.signable:
            return "待审签：补证后具备审签条件"
        if self.before.signable and not self.after.signable:
            return "待审签：复核后失去审签条件，暂缓"
        return "待审签：调价区间变化，需按新口径复核"


@dataclass
class RevisionReport:
    scenario: Scenario
    series_changes: list[SeriesChange]
    contract_impacts: list[ContractImpact]
    affected_series: dict[str, list[str]]
    affected_contracts: dict[str, list[str]]

    @property
    def publication_restorations(self) -> list[SeriesChange]:
        return [
            change
            for change in self.series_changes
            if change.before_published is False and change.after_published is True
        ]

    @property
    def value_changes(self) -> list[SeriesChange]:
        return [change for change in self.series_changes if change.published_value_changed]


def revision_report(
    bundle: Bundle,
    scenario: Scenario,
    baseline_catalog: dict[str, SeriesResult] | None = None,
    baseline_scenarios: list[Scenario] | None = None,
) -> RevisionReport:
    baseline_catalog = baseline_catalog or compile_catalog(bundle, BASELINE)
    scenario_catalog = compile_catalog(scenario.bundle, scenario.policy)
    changes: list[SeriesChange] = []
    affected_series: dict[str, list[str]] = {}
    for name, before in baseline_catalog.items():
        after = scenario_catalog[name]
        for period in sorted(before.periods):
            if period not in after.periods:
                continue
            left = before.periods[period]
            right = after.periods[period]
            change = SeriesChange(
                series=name,
                period=period,
                before_value=left.published_index,
                after_value=right.published_index,
                before_published=left.published,
                after_published=right.published,
                before_coverage=left.coverage,
                after_coverage=right.coverage,
            )
            if change.kind != "无变化":
                changes.append(change)
                affected_series.setdefault(name, []).append(period)

    impacts: list[ContractImpact] = []
    affected_contracts: dict[str, list[str]] = {}
    for name, contract in bundle.contracts.items():
        before = evaluate_contract(bundle, contract, baseline_catalog, "baseline")
        after = evaluate_contract(scenario.bundle, contract, scenario_catalog, scenario.name)
        impact = ContractImpact(contract=contract, before=before, after=after)
        impacts.append(impact)
        if impact.executable_changed or impact.source_changed or impact.signability_changed:
            affected_contracts.setdefault(name, []).append(impact.treatment)

    return RevisionReport(
        scenario=scenario,
        series_changes=changes,
        contract_impacts=impacts,
        affected_series=affected_series,
        affected_contracts=affected_contracts,
    )

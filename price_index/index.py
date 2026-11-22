"""规格可比、衔接验证与采购价格指数的编制。

编制口径（可在 Policy 中调整，默认即报告采用的口径）：

* 固定基期篮子：基期当月、同一区域与类别、状态为 regular 的成交记录。
* 权数为基期支出（单价 × 交易量），覆盖率也按基期支出份额计算。
* 每期只对能取到当期可比价格的篮子单元计算价格比；指数按已覆盖单元的
  基期支出重新归一，覆盖率不足最低要求时该期不得公示。
* 当期价格优先取本规格的 regular 成交；没有时，只有在 *已登记且状态为
  accepted* 的衔接关系覆盖该区域与月份、并且换算系数与重叠期实际成交比价
  相符时，才允许经衔接取得。accepted 但被重叠证据推翻的衔接不得使用。
* 整包与拆分规格不得对同一交易量重复计权：同一 linked_group 内若同时登记
  整包与拆分规格，按 package_policy 只保留一种表示。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP

from .model import Bridge, Bundle, Observation, SeriesDef, Spec

PUBLISH_QUANTUM = Decimal("0.0001")
COVERAGE_QUANTUM = Decimal("0.000001")


def _q(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 口径
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """一次编制的口径。默认值即基线口径。"""

    bridge_states: frozenset[str] = frozenset({"accepted"})
    require_bridge_evidence: bool = True
    evidence_tolerance: Decimal = Decimal("0.10")
    max_chain: int = 3
    weight: str = "expenditure"  # expenditure | quantity
    missing: str = "exclude"  # exclude | carry
    package_policy: str = "split"  # split | package
    min_coverage_override: Decimal | None = None
    include_states: frozenset[str] = frozenset({"regular"})

    def __post_init__(self) -> None:
        if self.weight not in {"expenditure", "quantity"}:
            raise ValueError(f"未知权数口径：{self.weight}")
        if self.missing not in {"exclude", "carry"}:
            raise ValueError(f"未知缺报处理：{self.missing}")
        if self.package_policy not in {"split", "package"}:
            raise ValueError(f"未知整包处理：{self.package_policy}")
        if self.max_chain < 1:
            raise ValueError("衔接链长度必须至少为 1")


BASELINE = Policy()


def with_bridge_state(bundle: Bundle, bridge_ids: set[str], state: str) -> Bundle:
    """返回只改变若干衔接状态的资料副本，用于复核情景。"""
    bridges = [
        replace(bridge, state=state) if bridge.id in bridge_ids else bridge
        for bridge in bundle.bridges
    ]
    return replace(bundle, bridges=bridges)


# ---------------------------------------------------------------------------
# 规格可比性
# ---------------------------------------------------------------------------


def spec_comparability(left: Spec, right: Spec) -> tuple[bool, str]:
    """判断两个规格能否互为替代；返回 (是否可比, 理由)。

    计量单位相同但包括项目不同（例如是否含交通）时，结构上可以经衔接换算，
    但必须依赖已登记且有证据的衔接。整包与单项（package 对 visit）只有包括
    项目完全一致时才可能换算，且同样必须有衔接。
    """
    if left.category != right.category:
        return False, "服务类别不同"
    if left.unit == right.unit:
        if left.components == right.components:
            return True, "计量单位与包括项目一致"
        return True, "计量单位相同、包括项目不同，须经衔接"
    if {left.unit, right.unit} == {"package", "visit"}:
        if left.components == right.components:
            return True, "整包与单项的包括项目一致，须经衔接"
        return False, "整包与单项的包括项目不一致，不能比较"
    return False, f"计量单位不同（{left.unit} / {right.unit}）"


# ---------------------------------------------------------------------------
# 衔接评估
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeAssessment:
    bridge: Bridge
    structural_ok: bool
    structural_reason: str
    evidence_pairs: int
    observed_ratio: Decimal | None
    reference_ratio: Decimal | None
    deviation: Decimal | None
    evidence_ok: bool
    usable: bool
    reason: str

    @property
    def status(self) -> str:
        if not self.structural_ok:
            return "结构不可比"
        if not self.evidence_ok:
            return "证据不符"
        if self.bridge.state == "accepted":
            return "可用"
        return f"未生效（{self.bridge.state}）"


def _bridge_evidence(
    bundle: Bundle, bridge: Bridge
) -> tuple[int, Decimal | None]:
    """用重叠期内同一交易条件下的实际成交比价核对换算系数。

    同类计量单位（例如均为每次）时，同一机构、同一区域、同一月份的两种规格
    报价即处于同一交易条件。整包与单项之间还须能对应到同一交易批次
    （linked_group 非空且相同），否则无法证明两笔报价描述的是同一批交易，
    不能据以换算。
    """
    left = bundle.specs[bridge.left]
    right = bundle.specs[bridge.right]
    requires_batch = {left.unit, right.unit} == {"package", "visit"}
    ratios: list[Decimal] = []
    for row in bundle.observations_between(bridge.first_period, bridge.last_period):
        if not row.priced:
            continue
        if bridge.valid_area not in (row.area, "*"):
            continue
        if row.spec != bridge.left:
            continue
        other = bundle.observation(row.period, row.area, row.supplier, bridge.right)
        if other is None or not other.priced:
            continue
        if requires_batch and (
            not row.linked_group or row.linked_group != other.linked_group
        ):
            continue
        ratios.append(other.price / row.price)
    if not ratios:
        return 0, None
    return len(ratios), sum(ratios) / len(ratios)


def assess_bridge(bundle: Bundle, bridge: Bridge, policy: Policy) -> BridgeAssessment:
    left, right = bundle.specs[bridge.left], bundle.specs[bridge.right]
    structural_ok, structural_reason = spec_comparability(left, right)
    pairs, observed = _bridge_evidence(bundle, bridge)
    requires_batch = {left.unit, right.unit} == {"package", "visit"}
    deviation = None
    evidence_ok = True
    if observed is not None:
        deviation = abs(bridge.rate / observed - 1)
        evidence_ok = deviation <= policy.evidence_tolerance
    elif requires_batch:
        # 整包与单项的换算必须有同一交易批次的对应证据，不能仅凭登记关系。
        evidence_ok = False

    if not structural_ok:
        reason = structural_reason
    elif bridge.state not in policy.bridge_states:
        reason = f"衔接状态为 {bridge.state}，当前口径不采用"
    elif observed is None and requires_batch:
        reason = (
            "整包与单项之间没有同一交易批次的对应成交，"
            "无法确认两笔报价描述同一批交易，不能作规格替代"
        )
    elif observed is None:
        reason = "无重叠成交可比价，按已登记关系采用"
    elif not evidence_ok:
        reason = (
            f"重叠期实际比价 {observed:.4f} 与登记系数 {bridge.rate} 相差 "
            f"{deviation:.2%}，超出容差 {policy.evidence_tolerance:.0%}，证据不足"
        )
    else:
        reason = (
            f"重叠期实际比价 {observed:.4f} 与登记系数相符（偏差 {deviation:.2%}）"
        )

    usable = (
        structural_ok
        and bridge.state in policy.bridge_states
        and (evidence_ok or not policy.require_bridge_evidence)
    )
    # 即便放宽证据要求，整包与单项在缺少批次证据时仍不得换算。
    if requires_batch and observed is None:
        usable = False
    return BridgeAssessment(
        bridge=bridge,
        structural_ok=structural_ok,
        structural_reason=structural_reason,
        evidence_pairs=pairs,
        observed_ratio=observed,
        reference_ratio=observed,
        deviation=deviation,
        evidence_ok=evidence_ok,
        usable=usable,
        reason=reason,
    )


def bridge_assessments(bundle: Bundle, policy: Policy = BASELINE) -> list[BridgeAssessment]:
    return [assess_bridge(bundle, bridge, policy) for bridge in bundle.bridges]


# ---------------------------------------------------------------------------
# 基期篮子
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BasketCell:
    area: str
    supplier: str
    spec: str
    base_id: str
    base_price: Decimal
    base_quantity: Decimal
    linked_group: str

    @property
    def expenditure(self) -> Decimal:
        return self.base_price * self.base_quantity

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.area, self.supplier, self.spec)


@dataclass
class Basket:
    series: SeriesDef
    cells: list[BasketCell]
    excluded: list[tuple[str, str]]  # (说明, 记录号)
    total_expenditure: Decimal
    total_quantity: Decimal

    def weight(self, cell: BasketCell, mode: str) -> Decimal:
        return cell.expenditure if mode == "expenditure" else cell.base_quantity


def build_basket(
    bundle: Bundle, series: SeriesDef, policy: Policy = BASELINE
) -> Basket:
    definition = series
    base_rows = [
        row
        for row in bundle.period_rows(definition.base)
        if row.state in policy.include_states and row.price is not None
    ]
    base_rows = [
        row
        for row in base_rows
        if (definition.area == "*" or row.area == definition.area)
        and bundle.specs[row.spec].category == definition.category
    ]
    excluded: list[tuple[str, str]] = []

    groups: dict[str, list[Observation]] = defaultdict(list)
    for row in base_rows:
        if row.linked_group:
            groups[row.linked_group].append(row)

    dropped: set[str] = set()
    for group, rows in groups.items():
        packages = [r for r in rows if bundle.specs[r.spec].is_package]
        splits = [r for r in rows if not bundle.specs[r.spec].is_package]
        if not packages or not splits:
            continue
        drop = packages if policy.package_policy == "split" else splits
        for row in drop:
            dropped.add(row.id)
            excluded.append(
                (
                    f"同一交易批次 {group} 的整包与拆分重复计权，"
                    f"按 {policy.package_policy} 口径剔除",
                    row.id,
                )
            )

    cells = [
        BasketCell(
            area=row.area,
            supplier=row.supplier,
            spec=row.spec,
            base_id=row.id,
            base_price=row.price,
            base_quantity=row.quantity,
            linked_group=row.linked_group,
        )
        for row in base_rows
        if row.id not in dropped
    ]
    cells.sort(key=lambda cell: (cell.area, cell.supplier, cell.spec))
    total_expenditure = sum((cell.expenditure for cell in cells), Decimal(0))
    total_quantity = sum((cell.base_quantity for cell in cells), Decimal(0))
    if total_expenditure <= 0:
        raise ValueError(f"系列 {series.series} 的基期篮子为空或支出为零")
    return Basket(
        series=definition,
        cells=cells,
        excluded=excluded,
        total_expenditure=total_expenditure,
        total_quantity=total_quantity,
    )


# ---------------------------------------------------------------------------
# 当期可比价格的取数轨迹
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """某一篮子单元在某一期的可比价格及其证据链。"""

    status: str
    price: Decimal | None = None
    source_id: str | None = None
    chain: tuple[tuple[str, str, str, Decimal, str], ...] = ()
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.price is not None


@dataclass
class PriceIndex:
    bundle: Bundle
    policy: Policy
    assessments: dict[str, BridgeAssessment] = field(default_factory=dict)

    @classmethod
    def create(cls, bundle: Bundle, policy: Policy = BASELINE) -> "PriceIndex":
        index = cls(bundle=bundle, policy=policy)
        index.assessments = {
            item.bridge.id: item for item in bridge_assessments(bundle, policy)
        }
        return index

    def resolve(
        self,
        period: str,
        area: str,
        supplier: str,
        spec: str,
        _seen: frozenset[str] | None = None,
        _depth: int = 0,
    ) -> Resolution:
        row = self.bundle.observation(period, area, supplier, spec)
        if row is not None and row.priced:
            return Resolution("本期成交", row.price, row.id, (), "状态为 regular")
        if row is not None:
            status = {
                "missing": "缺报",
                "trial": "仅有试行价",
                "promotion": "仅有优惠价",
            }.get(row.state, row.state)
            base_status = status
        else:
            base_status = self._absence_status(period, area, supplier)

        if _depth >= self.policy.max_chain:
            return Resolution(base_status, None, row.id if row else None, (), "未取得衔接")
        seen = _seen or frozenset({spec})
        for assessment in sorted(self.assessments.values(), key=lambda a: a.bridge.id):
            bridge = assessment.bridge
            if not assessment.usable:
                continue
            if not bridge.covers(area, period):
                continue
            if bridge.left == spec and bridge.right not in seen:
                inner = self.resolve(
                    period,
                    area,
                    supplier,
                    bridge.right,
                    seen | {bridge.right},
                    _depth + 1,
                )
                if inner.available:
                    price = inner.price / bridge.rate
                    step = (bridge.id, bridge.left, bridge.right, bridge.rate, "divide")
                    return Resolution(
                        "经衔接",
                        price,
                        inner.source_id,
                        (step,) + inner.chain,
                        f"{spec} 经 {bridge.id} 由 {bridge.right} 换算",
                    )
            if bridge.right == spec and bridge.left not in seen:
                inner = self.resolve(
                    period,
                    area,
                    supplier,
                    bridge.left,
                    seen | {bridge.left},
                    _depth + 1,
                )
                if inner.available:
                    price = inner.price * bridge.rate
                    step = (bridge.id, bridge.left, bridge.right, bridge.rate, "multiply")
                    return Resolution(
                        "经衔接",
                        price,
                        inner.source_id,
                        (step,) + inner.chain,
                        f"{spec} 经 {bridge.id} 由 {bridge.left} 换算",
                    )
        rejected = self._bridge_candidates(period, area, spec)
        detail = self._absence_detail(period, area, supplier, spec, base_status)
        if rejected:
            detail += "；相关衔接：" + "；".join(rejected)
        return Resolution(
            base_status,
            None,
            row.id if row else None,
            (),
            detail,
        )

    def _bridge_candidates(self, period: str, area: str, spec: str) -> list[str]:
        """列出覆盖该期、但不可用的衔接，说明缺少可比价格的原因。"""
        notes: list[str] = []
        for assessment in sorted(self.assessments.values(), key=lambda a: a.bridge.id):
            bridge = assessment.bridge
            if spec not in (bridge.left, bridge.right):
                continue
            if not bridge.covers(area, period):
                continue
            notes.append(f"{bridge.id} {assessment.status}（{assessment.reason}）")
        return notes

    def _absence_status(self, period: str, area: str, supplier: str) -> str:
        presence = self.bundle.presence(period, area, supplier)
        if presence == "尚未开点":
            return "机构尚未开点"
        if presence == "已停点":
            return "机构已停点"
        if presence == "临时未报":
            return "机构临时未报"
        return "本规格本期无记录"

    def _absence_detail(
        self, period: str, area: str, supplier: str, spec: str, status: str
    ) -> str:
        presence = self.bundle.presence(period, area, supplier)
        if presence == "尚未开点":
            return f"{supplier} 在 {area} 至 {period} 尚未开点（网点未开始经营）"
        if presence == "已停点":
            return f"{supplier} 在 {area} 已停点，{period} 之后无该网点记录"
        if presence == "临时未报":
            return f"{supplier} 在 {area} 于 {period} 整月无记录（临时未报，非停点）"
        if status == "本规格本期无记录":
            return f"{supplier} 在 {area} 于 {period} 在点但未提供 {spec}（停项或未开展）"
        if status == "缺报":
            return "收到缺报通知，无成交证据"
        if status == "仅有试行价":
            return "仅有试行价，未作为常规成交"
        if status == "仅有优惠价":
            return "仅有优惠价，未作为常规成交"
        return status


# ---------------------------------------------------------------------------
# 系列编制
# ---------------------------------------------------------------------------


@dataclass
class CellTrace:
    cell: BasketCell
    weight: Decimal
    quantity_weight: Decimal
    resolution: Resolution
    ratio: Decimal | None
    contribution: Decimal | None  # 对未归一价格和的贡献（权数 × 价格比）

    @property
    def covered(self) -> bool:
        return self.ratio is not None


@dataclass
class PeriodResult:
    series: str
    period: str
    coverage: Decimal
    index: Decimal | None  # 已归一的可比价格指数（未按公示位数取整）
    published: bool
    published_index: Decimal | None
    traces: list[CellTrace]
    missing: list[CellTrace]
    carry_used: bool = False

    @property
    def change_from_base(self) -> Decimal | None:
        if self.published_index is None:
            return None
        return self.published_index / Decimal(100) - 1


@dataclass
class SeriesResult:
    series: SeriesDef
    policy: Policy
    basket: Basket
    periods: dict[str, PeriodResult]
    min_coverage: Decimal
    runs: list[tuple[str, str]]
    breaks: list[dict]

    def period(self, period: str) -> PeriodResult:
        return self.periods[period]

    def published(self, period: str) -> bool:
        result = self.periods.get(period)
        return bool(result and result.published)

    def value(self, period: str) -> Decimal | None:
        result = self.periods.get(period)
        return result.published_index if result else None


def _contiguous_runs(periods: list[str], published: set[str]) -> list[tuple[str, str]]:
    runs: list[tuple[str, str]] = []
    start = None
    previous = None
    for period in periods:
        if period in published:
            if start is None:
                start = period
            previous = period
        elif start is not None:
            runs.append((start, previous))
            start = previous = None
    if start is not None:
        runs.append((start, previous))
    return runs


def compile_series(
    bundle: Bundle, series: str, policy: Policy = BASELINE
) -> SeriesResult:
    definition = bundle.series[series]
    basket = build_basket(bundle, definition, policy)
    engine = PriceIndex.create(bundle, policy)
    min_coverage = (
        policy.min_coverage_override
        if policy.min_coverage_override is not None
        else definition.min_coverage
    )

    periods = [p for p in bundle.periods if p >= definition.base]
    period_results: dict[str, PeriodResult] = {}
    previous_prices: dict[tuple[str, str, str], Decimal] = {}

    for period in periods:
        traces: list[CellTrace] = []
        price_sum = Decimal(0)
        covered_expenditure = Decimal(0)
        covered_weight = Decimal(0)
        carry_used = False
        for cell in basket.cells:
            resolution = engine.resolve(period, cell.area, cell.supplier, cell.spec)
            ratio = None
            if resolution.available and cell.base_price > 0:
                ratio = resolution.price / cell.base_price
            elif policy.missing == "carry" and period != definition.base:
                fallback = previous_prices.get(cell.key)
                if fallback is not None:
                    ratio = fallback / cell.base_price
                    resolution = Resolution(
                        "沿用前期价",
                        fallback,
                        resolution.source_id,
                        resolution.chain,
                        "缺报，按敏感性口径沿用最近可得价格",
                    )
                    carry_used = True
            weight = basket.weight(cell, policy.weight)
            contribution = weight * ratio if ratio is not None else None
            traces.append(
                CellTrace(
                    cell=cell,
                    weight=weight,
                    quantity_weight=cell.base_quantity,
                    resolution=resolution,
                    ratio=ratio,
                    contribution=contribution,
                )
            )
            if ratio is not None:
                price_sum += weight * ratio
                covered_weight += weight
                covered_expenditure += cell.expenditure
                previous_prices[cell.key] = resolution.price

        # 覆盖率始终按基期支出份额衡量，与权数口径无关。
        coverage = covered_expenditure / basket.total_expenditure
        coverage = _q(coverage, COVERAGE_QUANTUM)
        raw_index = (
            Decimal(100) * price_sum / covered_weight
            if covered_weight > 0
            else None
        )
        published = (
            raw_index is not None
            and coverage >= min_coverage
            and (period == definition.base or covered_expenditure > 0)
        )
        published_index = (
            _q(raw_index, PUBLISH_QUANTUM) if published and raw_index is not None else None
        )
        period_results[period] = PeriodResult(
            series=series,
            period=period,
            coverage=coverage,
            index=raw_index,
            published=published,
            published_index=published_index,
            traces=traces,
            missing=[trace for trace in traces if not trace.covered],
            carry_used=carry_used,
        )

    published_periods = {p for p, result in period_results.items() if result.published}
    runs = _contiguous_runs(periods, published_periods)
    breaks = _build_breaks(period_results, min_coverage)
    return SeriesResult(
        series=definition,
        policy=policy,
        basket=basket,
        periods=period_results,
        min_coverage=min_coverage,
        runs=runs,
        breaks=breaks,
    )


def _build_breaks(
    periods: dict[str, PeriodResult], min_coverage: Decimal
) -> list[dict]:
    """标出公示状态发生变化的月份，并给出无法衔接的单元及原因。"""
    breaks: list[dict] = []
    ordered = sorted(periods)
    previous = None
    for period in ordered:
        result = periods[period]
        if previous is not None and result.published != previous.published:
            breaks.append(
                {
                    "period": period,
                    "transition": "恢复公示" if result.published else "中断公示",
                    "coverage": result.coverage,
                    "min_coverage": min_coverage,
                    "missing": [
                        {
                            "area": trace.cell.area,
                            "supplier": trace.cell.supplier,
                            "spec": trace.cell.spec,
                            "base_id": trace.cell.base_id,
                            "status": trace.resolution.status,
                            "detail": trace.resolution.detail,
                            "weight_share": _q(
                                trace.weight
                                / sum(t.weight for t in result.traces),
                                Decimal("0.000001"),
                            ),
                        }
                        for trace in result.missing
                    ],
                }
            )
        previous = result
    return breaks


# ---------------------------------------------------------------------------
# 构成偏差的对照（用于回答“占比变高是否被当作涨价”）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositionComparison:
    """同一批可比单元上，固定篮子指数与两种“朴素”价格指标的差异。"""

    period: str
    fixed_basket: Decimal | None
    unit_value: Decimal | None
    simple_average: Decimal | None

    @property
    def unit_value_gap(self) -> Decimal | None:
        if self.fixed_basket is None or self.unit_value is None:
            return None
        return self.unit_value - self.fixed_basket

    @property
    def simple_average_gap(self) -> Decimal | None:
        if self.fixed_basket is None or self.simple_average is None:
            return None
        return self.simple_average - self.fixed_basket


def composition_comparison(
    bundle: Bundle, series: str, period: str, policy: Policy = BASELINE
) -> CompositionComparison:
    result = compile_series(bundle, series, policy)
    cell_result = result.period(period)
    covered = [trace for trace in cell_result.traces if trace.covered]
    fixed = cell_result.index
    if not covered:
        return CompositionComparison(period, fixed, None, None)

    observations: list[tuple[Decimal, Decimal, Decimal]] = []
    for trace in covered:
        row = bundle.observation(
            period, trace.cell.area, trace.cell.supplier, trace.cell.spec
        )
        price = trace.resolution.price
        quantity = row.quantity if row is not None and row.quantity is not None else trace.cell.base_quantity
        observations.append((price, quantity, trace.cell.base_price))

    current_quantity = sum((quantity for _, quantity, _ in observations), Decimal(0))
    unit_value = (
        Decimal(100)
        * sum((price * quantity for price, quantity, _ in observations), Decimal(0))
        / sum(
            (
                base_price * quantity
                for (_, quantity, base_price) in observations
            ),
            Decimal(0),
        )
        if current_quantity > 0
        else None
    )
    simple_average = Decimal(100) * sum(
        (price / base_price for price, _, base_price in observations), Decimal(0)
    ) / len(observations)
    return CompositionComparison(
        period=period,
        fixed_basket=fixed,
        unit_value=unit_value,
        simple_average=simple_average,
    )


# ---------------------------------------------------------------------------
# 篮子构成
# ---------------------------------------------------------------------------


def basket_composition(result: SeriesResult) -> dict:
    """按区域、机构、规格汇总基期权数，便于核对支出结构。"""
    by_area: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    by_supplier: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    by_spec: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    total = result.basket.total_expenditure
    for cell in result.basket.cells:
        by_area[cell.area] += cell.expenditure
        by_supplier[cell.supplier] += cell.expenditure
        by_spec[cell.spec] += cell.expenditure

    def shares(mapping):
        return {
            key: {
                "expenditure": value,
                "share": _q(value / total, Decimal("0.000001")),
            }
            for key, value in sorted(mapping.items())
        }

    return {
        "series": result.series.series,
        "base": result.series.base,
        "total_expenditure": total,
        "cells": len(result.basket.cells),
        "excluded": result.basket.excluded,
        "by_area": shares(by_area),
        "by_supplier": shares(by_supplier),
        "by_spec": shares(by_spec),
    }

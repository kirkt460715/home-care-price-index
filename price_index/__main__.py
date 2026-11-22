"""居家服务采购价格指数与合同调价的命令行入口。

用法:
    python3 -m price_index report
    python3 -m price_index summary
    python3 -m price_index series IN
    python3 -m price_index weights IA
    python3 -m price_index bridges
    python3 -m price_index contracts
    python3 -m price_index revisions
    python3 -m price_index trace IN 2026-06
    python3 -m price_index composition IN 2026-06
    python3 -m price_index splice
    python3 -m price_index issued
    python3 -m price_index json > result.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from .contracts import (
    compile_catalog,
    evaluate_contract,
    reconcile_issued,
    review_contracts,
    revision_report,
    splicing_counterexample,
    standard_scenarios,
)
from .index import BASELINE, bridge_assessments, compile_series
from .model import DATA, load_bundle

CONTRACT_STATES = {"pending": "待审签", "issued": "已审签"}


def _fmt(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{places}f}"


def _pct(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{places}f}%"


def _bundle(path: str | None):
    return load_bundle(Path(path) if path else DATA)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def cmd_summary(args) -> int:
    bundle = _bundle(args.data)
    catalog = compile_catalog(bundle, BASELINE)
    print("系列编制概览（基线口径）")
    print(
        f"{'系列':<4}{'区域':<7}{'类别':<6}{'基期':<9}{'最低覆盖':>9}"
        f"{'已公示期':>10}{'中断期':>8}"
    )
    for name, result in catalog.items():
        published = sum(1 for p in result.periods.values() if p.published)
        broken = len(result.periods) - published
        print(
            f"{name:<4}{result.series.area:<7}{result.series.category:<6}"
            f"{result.series.base:<9}{_pct(result.min_coverage):>9}"
            f"{published:>10}{broken:>8}"
        )
        for start, end in result.runs:
            print(f"    连续公示 {start} → {end}")
        for item in result.breaks:
            missing = ", ".join(
                f"{m['area']}/{m['supplier']}/{m['spec']}({m['status']})"
                for m in item["missing"][:3]
            )
            print(
                f"    {item['transition']} {item['period']}"
                f"（覆盖 {_pct(item['coverage'])}）：{missing or '无缺项'}"
            )
    return 0


def cmd_series(args) -> int:
    bundle = _bundle(args.data)
    result = compile_series(bundle, args.series, BASELINE)
    print(f"系列 {args.series}（{result.series.area}/{result.series.category}，基期 {result.series.base}）")
    print(f"{'月份':<9}{'指数':>10}{'覆盖':>9}{'状态':>10}{'较基期':>10}")
    for period in sorted(result.periods):
        item = result.periods[period]
        print(
            f"{period:<9}{_fmt(item.published_index, 4):>10}"
            f"{_pct(item.coverage):>9}"
            f"{'公示' if item.published else '中断':>10}"
            f"{_pct(item.change_from_base):>10}"
        )
    if args.missing:
        print("\n未计入的篮子单元")
        for period in sorted(result.periods):
            item = result.periods[period]
            if not item.missing:
                continue
            print(f"  {period}")
            for trace in item.missing:
                print(
                    f"    {trace.cell.area}/{trace.cell.supplier}/{trace.cell.spec}"
                    f" 权重 {_pct(trace.weight / result.basket.total_expenditure)}"
                    f"：{trace.resolution.status} — {trace.resolution.detail}"
                )
    return 0


def cmd_bridges(args) -> int:
    bundle = _bundle(args.data)
    print("衔接记录评估（基线口径：accepted，须有重叠证据）")
    for item in bridge_assessments(bundle, BASELINE):
        bridge = item.bridge
        print(
            f"{bridge.id} {bridge.state:<9} {bridge.valid_area:<5}"
            f"{bridge.left} → {bridge.right}  系数 {bridge.rate}"
            f"  窗口 {bridge.first_period}~{bridge.last_period}"
        )
        print(
            f"    可比：{item.structural_reason}；重叠样本 {item.evidence_pairs} 组"
            f"；实际比价 {_fmt(item.observed_ratio, 4)}"
            f"；偏差 {_pct(item.deviation)}"
        )
        print(f"    结论：{item.status} — {item.reason}")
    return 0


def cmd_contracts(args) -> int:
    bundle = _bundle(args.data)
    reviews, scenarios = review_contracts(bundle)
    print("合同审签区间")
    print(
        "  可审签区间 = 基线口径与补证后可成立情景的金额范围；"
        "纯口径敏感度单列，不进入区间。"
    )
    for name, review in reviews.items():
        contract = review.contract
        print(
            f"\n{contract.contract}  状态 {CONTRACT_STATES.get(contract.state, contract.state)}"
            f"  主系列 {contract.series}  备选 {contract.fallback or '无'}"
            f"  参考单价 {_fmt(contract.reference_price)}  限幅 ±{_pct(contract.max_change, 1)}"
        )
        selected = [
            item
            for item in review.outcomes
            if not args.scenario or item.scenario == args.scenario
        ]
        for item in selected:
            if item.scenario in review.contingent_names:
                kind = "补证后"
            elif item.scenario in review.admissible_names:
                kind = "正式"
            else:
                kind = "敏感性"
            if item.signable:
                print(
                    f"  [{kind} {item.scenario:<24}] 可审签 {item.series_used}"
                    f" {item.base.index}→{item.end.index} 比例 {_fmt(item.ratio_applied, 6)}"
                    f" 金额 {_fmt(item.amount)}"
                    + ("（已限幅）" if item.cap_binding != "none" else "")
                )
            else:
                detail = item.blockers[0]["detail"] if item.blockers else "不可审签"
                print(f"  [{kind} {item.scenario:<24}] 暂缓：{detail}")
        interval = review.ratio_interval
        amounts = review.amount_interval
        if interval is not None:
            print(
                f"  可审签调价比例区间 {_fmt(interval[0], 6)}~{_fmt(interval[1], 6)}；"
                f"金额区间 {_fmt(amounts[0])}~{_fmt(amounts[1])}"
                f"（限幅内：{'是' if review.interval_within_cap() else '否'}）"
            )
        elif review.baseline.blockers:
            print("  可审签区间：无（当前没有符合条件的情景）")
        contingent = [
            item
            for item in review.contingent_outcomes
            if item.signable and item.amount is not None
        ]
        if contingent:
            low = min(item.amount for item in contingent)
            high = max(item.amount for item in contingent)
            print(
                f"  补证后金额范围 {_fmt(low)}~{_fmt(high)}"
                "（须先完成衔接审定或恢复登记，不是今天可审签）"
            )
        sensitivity = [
            item
            for item in review.sensitivity_outcomes
            if item.signable and item.amount is not None
        ]
        if sensitivity:
            low = min(item.amount for item in sensitivity)
            high = max(item.amount for item in sensitivity)
            print(f"  敏感性金额范围 {_fmt(low)}~{_fmt(high)}（仅参考，不用于审签）")
        evidence = review.blocking_evidence(args.scenario or None)
        if evidence:
            print("  必要补证：")
            for item in evidence:
                print(f"    - {item}")
        if any(
            not item.signable
            for item in selected
            if item.scenario in review.admissible_names
        ):
            print(
                "  暂缓条款：合同窗口内主序列未公示，不得以相邻月份或另一序列"
                "基期替代；须补齐上述证据后重新计算。"
            )
        if contract.state == "issued" and contract.contract in bundle.issued:
            recorded = bundle.issued[contract.contract]
            print(
                f"  已审签记录：{recorded.series}"
                f" {recorded.base_observed}→{recorded.end_observed}"
                f" 金额 {_fmt(recorded.amount)}（{recorded.issued_on}）"
                "；本次计算只作对照，实际支付以当时采用的指数与条款为准"
            )
        notes = {note for item in selected for note in item.notes}
        for note in sorted(notes):
            print(f"  说明：{note}")
    return 0


def cmd_revisions(args) -> int:
    bundle = _bundle(args.data)
    scenarios = standard_scenarios(bundle)
    for scenario in scenarios:
        if scenario.name == "baseline":
            continue
        report = revision_report(bundle, scenario)
        if not report.series_changes and not report.affected_contracts:
            continue
        kind_label = {
            "sensitivity": "敏感性",
            "contingent": "补证后",
            "baseline": "正式",
        }.get(scenario.kind, scenario.kind)
        print(f"\n情景 {scenario.name}（{kind_label}）：{scenario.description}")
        restored = [c for c in report.publication_restorations]
        values = [c for c in report.value_changes]
        if restored:
            print("  恢复公示：")
            for change in restored:
                print(
                    f"    {change.series} {change.period}"
                    f" 覆盖 {_pct(change.before_coverage)} → {_pct(change.after_coverage)}"
                )
        if values:
            print("  公示值改变：")
            for change in values:
                print(
                    f"    {change.series} {change.period}"
                    f" {_fmt(change.before_value, 4)} → {_fmt(change.after_value, 4)}"
                )
        if report.affected_contracts:
            print("  合同影响：")
            for contract, treatments in report.affected_contracts.items():
                print(f"    {contract}：{'；'.join(treatments)}")
        else:
            print("  合同影响：无（仅影响未用于任何合同窗口的月份或系列）")
    return 0


def cmd_trace(args) -> int:
    bundle = _bundle(args.data)
    result = compile_series(bundle, args.series, BASELINE)
    period = result.periods.get(args.period)
    if period is None:
        print(f"系列 {args.series} 在 {args.period} 没有编制结果", file=sys.stderr)
        return 2
    print(
        f"系列 {args.series} {args.period} 指数 {_fmt(period.published_index, 4)}"
        f"（覆盖 {_pct(period.coverage)}，{'公示' if period.published else '中断'}）"
    )
    total = sum(trace.weight for trace in period.traces)
    print(
        f"{'区域/机构/规格':<24}{'基期记录':<16}{'基期价':>8}{'权数':>10}"
        f"{'权重':>8}{'当期可比价':>14}  {'来源':<18}{'状态'}"
    )
    for trace in period.traces:
        cell = trace.cell
        price = trace.resolution.price
        label = f"{cell.area}/{cell.supplier}/{cell.spec}"
        share = trace.weight / total if total > 0 else Decimal(0)
        print(
            f"{label:<24}{cell.base_id:<16}{_fmt(cell.base_price, 0):>8}"
            f"{_fmt(trace.weight, 0):>10}{_pct(share):>8}{_fmt(price, 2):>14}"
            f"  {(trace.resolution.source_id or '（无）'):<18}{trace.resolution.status}"
        )
        if trace.resolution.chain:
            for step in trace.resolution.chain:
                print(
                    f"    衔接 {step[0]}：{step[1]} → {step[2]}"
                    f" 系数 {step[3]}（{step[4]}）"
                )
        elif trace.resolution.detail and not trace.covered:
            print(f"    {trace.resolution.detail}")
    return 0


def cmd_composition(args) -> int:
    from .index import composition_comparison

    bundle = _bundle(args.data)
    result = composition_comparison(bundle, args.series, args.period)
    print(f"系列 {args.series} {args.period} 口径对照")
    print(f"  固定篮子指数：{_fmt(result.fixed_basket, 4)}")
    print(f"  单位值指数：  {_fmt(result.unit_value, 4)}（差 {_fmt(result.unit_value_gap, 4)}）")
    print(f"  简单算术平均：{_fmt(result.simple_average, 4)}（差 {_fmt(result.simple_average_gap, 4)}）")
    print("  说明：固定篮子按基期支出加权；单位值与简单平均会随高价规格占比上升而抬高，不用于合同调价。")
    return 0


def cmd_splice(args) -> int:
    bundle = _bundle(args.data)
    result = splicing_counterexample(bundle, args.primary, args.fallback, args.period)
    if not result["ok"]:
        print(result["reason"], file=sys.stderr)
        return 2
    print("混接序列的错误示范（不得用于合同）")
    print(
        f"  {result['primary']} 在 {result['last_primary_period']} 的公示值："
        f"{_fmt(result['primary_value'], 4)}"
    )
    print(
        f"  {result['fallback']} 在 {result['period']} 的公示值："
        f"{_fmt(result['fallback_value'], 4)}"
    )
    print(f"  若直接相接，比价 {_fmt(result['naive_spliced_ratio'], 6)}")
    print(
        f"  {result['fallback']} 自身从基期 {result['fallback_base_period']}"
        f"（{_fmt(result['fallback_base_value'], 4)}）到 {result['period']} 的比价："
        f"{_fmt(result['true_same_series_ratio'], 6)}"
    )
    print(
        f"  基期差额被误算成 {_fmt(result['base_difference'], 6)}"
        f"（{_pct(result['base_difference'])}），不是价格变动。"
    )
    print("  合同 fallback 必须在窗口两端整体采用同一序列，不得混接。")
    return 0


def cmd_issued(args) -> int:
    from .contracts import Corrections

    bundle = _bundle(args.data)
    catalog = compile_catalog(bundle, BASELINE)
    corrections = []
    for item in getattr(args, "correction", []) or []:
        contract, series, period, old, new, recorded_on = item
        if contract not in bundle.contracts:
            print(f"未登记的合同：{contract}", file=sys.stderr)
            return 2
        corrections.append(
            Corrections(
                series=series,
                period=period,
                old_value=Decimal(old),
                new_value=Decimal(new),
                recorded_on=recorded_on,
                reference=contract,
            )
        )
    print("已审签结果回查")
    for contract in bundle.contracts.values():
        current = evaluate_contract(bundle, contract, catalog)
        result = reconcile_issued(bundle, contract, current, corrections)
        if result is None:
            continue
        print(
            f"\n{contract.contract}（{CONTRACT_STATES.get(contract.state, contract.state)}）"
            f" 当时采用 {result.recorded_series}"
            f" {result.recorded_base} → {result.recorded_end}"
            f" 金额 {_fmt(result.recorded_amount)}（{result.recorded_on}）"
        )
        print(
            f"  本次重算 采用 {current.series_used or '—'}"
            f" 金额 {_fmt(current.amount)}"
            f" 差额 {_fmt(result.amount_difference)}"
        )
        print(
            f"  公示值更正 {result.published_value_changed}；"
            f"序列变化 {result.source_changed}；"
            f"可执行调价变化 {result.executable_changed}"
        )
        for note in result.notes:
            print(f"  说明：{note}")
        print(f"  结论：{result.reopening}")
    return 0


def cmd_weights(args) -> int:
    from .index import basket_composition

    bundle = _bundle(args.data)
    composition = basket_composition(compile_series(bundle, args.series, BASELINE))
    print(
        f"系列 {args.series} 基期 {composition['base']} 篮子："
        f"{composition['cells']} 个单元，基期支出合计 {_fmt(composition['total_expenditure'])}"
    )
    for dimension, label in (
        ("by_area", "区域"),
        ("by_supplier", "机构"),
        ("by_spec", "规格"),
    ):
        print(f"  按{label}：")
        for key, item in composition[dimension].items():
            print(
                f"    {key:<12} 支出 {_fmt(item['expenditure']):>10}"
                f" 权重 {_pct(item['share'])}"
            )
    if composition["excluded"]:
        print("  已剔除（避免重复计权）：")
        for reason, record_id in composition["excluded"]:
            print(f"    {record_id}：{reason}")
    else:
        print("  已剔除：无（基期各交易批次均只有一种规格表示）")
    return 0


def cmd_report(args) -> int:
    """按连续期间、区域和服务类别生成绩效说明。"""
    bundle = _bundle(args.data)
    catalog = compile_catalog(bundle, BASELINE)
    reviews, _ = review_contracts(bundle)

    print("采购价格指数编制与合同调价报告")
    print("=" * 40)
    print("\n一、编制口径")
    print("  - 固定基期篮子，权数为基期支出（单价 × 交易量）。")
    print("  - 只有状态为 regular 的成交进入常规价格；缺报、试行价、优惠价单列。")
    print("  - 规格替代只采用已登记、状态 accepted 且与重叠成交比价相符的衔接；")
    print("    整包与单项还必须对应同一交易批次。")
    print("  - 覆盖率低于系列最低要求时该期中断公示，不用相邻月份或另一序列补齐。")

    print("\n二、可比较的支出权重")
    for name in catalog:
        from .index import basket_composition

        composition = basket_composition(catalog[name])
        parts = "，".join(
            f"{key} {_pct(item['share'])}"
            for key, item in composition["by_spec"].items()
        )
        print(
            f"  {name}（{catalog[name].series.area}/{catalog[name].series.category}，"
            f"基期 {catalog[name].series.base}）：支出 {_fmt(composition['total_expenditure'])}；"
            f"规格权重 {parts}"
        )

    print("\n三、指数变动与断点")
    for name, result in catalog.items():
        print(
            f"\n  {name}（{result.series.area}/{result.series.category}，"
            f"最低覆盖 {_pct(result.min_coverage)}）"
        )
        for start, end in result.runs:
            start_value = result.value(start)
            end_value = result.value(end)
            change = (
                (end_value / start_value - 1)
                if start_value and end_value and start != end
                else None
            )
            print(
                f"    连续公示 {start} → {end}："
                f"{_fmt(start_value, 4)} → {_fmt(end_value, 4)}"
                + (f"（变动 {_pct(change)}）" if change is not None else "")
            )
        for item in result.breaks:
            if item["transition"] == "中断公示":
                reasons = "；".join(
                    f"{m['area']}/{m['supplier']}/{m['spec']} {m['status']}"
                    for m in item["missing"][:4]
                )
                print(
                    f"    断点 {item['period']}：覆盖 {_pct(item['coverage'])}"
                    f" < {_pct(item['min_coverage'])}；{reasons or '无缺项'}"
                )

    print("\n四、合同审签与暂缓")
    for name, review in reviews.items():
        contract = review.contract
        state = CONTRACT_STATES.get(contract.state, contract.state)
        if review.baseline.signable:
            evaluation = review.baseline
            print(
                f"  {name}（{state}）：可审签；采用 {evaluation.series_used}"
                f" {evaluation.base.index} → {evaluation.end.index}；"
                f"比例 {_fmt(evaluation.ratio_applied, 6)}；金额 {_fmt(evaluation.amount)}"
            )
            interval = review.ratio_interval
            amounts = review.amount_interval
            if interval and amounts:
                print(
                    f"      可审签区间（含补证后可成立情景） 比例 "
                    f"{_fmt(interval[0], 6)}~{_fmt(interval[1], 6)}；"
                    f"金额 {_fmt(amounts[0])}~{_fmt(amounts[1])}"
                )
            contingent = [
                item.amount
                for item in review.contingent_outcomes
                if item.signable and item.amount is not None
            ]
            if contingent:
                print(
                    f"      其中补证后才成立 {_fmt(min(contingent))}~{_fmt(max(contingent))}"
                )
            sensitivity = [
                item.amount
                for item in review.sensitivity_outcomes
                if item.signable and item.amount is not None
            ]
            if sensitivity:
                print(
                    f"      敏感性金额范围 {_fmt(min(sensitivity))}~{_fmt(max(sensitivity))}"
                    "（不进入审签）"
                )
        else:
            print(f"  {name}（{state}）：暂缓；{review.baseline.blockers[0]['detail']}")
            for evidence in review.blocking_evidence()[:4]:
                print(f"      补证：{evidence}")
            contingent = [
                (item.scenario, item.amount)
                for item in review.contingent_outcomes
                if item.signable and item.amount is not None
            ]
            if contingent:
                choices = "；".join(
                    f"{scenario} 约 {_fmt(amount)}" for scenario, amount in contingent
                )
                print(f"      补证后可审签：{choices}（完成前仍为暂缓）")
            print(
                "      暂缓条款：主序列端点未公示时不得混接另一序列基期；"
                "须补齐证据后重新计算。"
            )
        notes = {
            note
            for item in review.outcomes
            if item.scenario in review.admissible_names
            for note in item.notes
            if "限幅" in note
        }
        for note in sorted(notes):
            print(f"      说明：{note}")

    print("\n五、规格处理的敏感程度")
    baseline_amounts = {
        name: review.baseline.amount
        for name, review in reviews.items()
        if review.baseline.amount is not None
    }
    for scenario_name in (
        "missing_carry",
        "quantity_weight",
        "package_weighted",
        "accept_filed_with_evidence",
        "renew_B2_through_2026_08",
        "coverage_70pct",
        "no_bridges",
    ):
        report = revision_report(
            bundle, next(s for s in standard_scenarios(bundle) if s.name == scenario_name)
        )
        impacts = []
        for impact in report.contract_impacts:
            before = baseline_amounts.get(impact.contract.contract)
            after = impact.after.amount
            if before is None or after is None or before == after:
                continue
            impacts.append(
                f"{impact.contract.contract} {_fmt(before)}→{_fmt(after)}"
            )
        label = {
            "missing_carry": "缺报沿用前价",
            "quantity_weight": "改用交易量权数",
            "package_weighted": "整包代替拆分",
            "accept_filed_with_evidence": "补登 B4 后",
            "renew_B2_through_2026_08": "B2 续登后",
            "coverage_70pct": "门槛降至 70%",
            "no_bridges": "完全不使用衔接",
        }.get(scenario_name, scenario_name)
        print(
            f"  {label}（{scenario_name}）："
            + ("；".join(impacts) if impacts else "不改变任何可审签金额")
        )

    print("\n六、结论")
    print("  1. 同一机构单价未变但计入两人协作与上门交通，属规格口径变化，")
    print("     应以可比规格的固定篮子指数判断，不可直接把新合同单价当涨价。")
    print("  2. 套餐与单项只在有同一交易批次的价格证据时比较；同一交易量不得")
    print("     同时作为整包与拆分的权重。")
    print("  3. 主序列端点未公示时，按合同约定整体转用备选序列；不得混接基期。")
    print("  4. 已审签单价以当时采用的指数与条款为准；只有正式登记的公示值更正")
    print("     才形成新的差额处理。")
    return 0


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value


def cmd_json(args) -> int:
    from .index import basket_composition

    bundle = _bundle(args.data)
    catalog = compile_catalog(bundle, BASELINE)
    reviews, scenarios = review_contracts(bundle)
    payload = {
        "series": {
            name: {
                "definition": asdict(result.series),
                "min_coverage": result.min_coverage,
                "basket": _jsonable(basket_composition(result)),
                "runs": result.runs,
                "breaks": result.breaks,
                "periods": {
                    period: {
                        "index": item.published_index,
                        "coverage": item.coverage,
                        "published": item.published,
                        "missing": [
                            {
                                "area": trace.cell.area,
                                "supplier": trace.cell.supplier,
                                "spec": trace.cell.spec,
                                "base_id": trace.cell.base_id,
                                "status": trace.resolution.status,
                                "detail": trace.resolution.detail,
                                "weight": trace.weight,
                            }
                            for trace in item.missing
                        ],
                    }
                    for period, item in result.periods.items()
                },
            }
            for name, result in catalog.items()
        },
        "bridge_assessments": [
            {
                "id": item.bridge.id,
                "state": item.bridge.state,
                "left": item.bridge.left,
                "right": item.bridge.right,
                "valid_area": item.bridge.valid_area,
                "first_period": item.bridge.first_period,
                "last_period": item.bridge.last_period,
                "rate": item.bridge.rate,
                "observed_ratio": item.observed_ratio,
                "deviation": item.deviation,
                "structural": item.structural_reason,
                "usable": item.usable,
                "status": item.status,
                "reason": item.reason,
            }
            for item in bridge_assessments(bundle, BASELINE)
        ],
        "contracts": {
            name: {
                "definition": asdict(review.contract),
                "signable": review.baseline.signable,
                "ratio_interval": review.ratio_interval,
                "amount_interval": review.amount_interval,
                "contingent_amounts": [
                    item.amount
                    for item in review.contingent_outcomes
                    if item.signable and item.amount is not None
                ],
                "blocking_evidence": review.blocking_evidence(),
                "outcomes": [
                    {
                        "scenario": item.scenario,
                        "kind": (
                            "contingent"
                            if item.scenario in review.contingent_names
                            else "baseline"
                            if item.scenario in review.admissible_names
                            else "sensitivity"
                        ),
                        "signable": item.signable,
                        "series_used": item.series_used,
                        "fallback_used": item.fallback_used,
                        "base": asdict(item.base) if item.base else None,
                        "end": asdict(item.end) if item.end else None,
                        "ratio_raw": item.ratio_raw,
                        "ratio_applied": item.ratio_applied,
                        "amount": item.amount,
                        "cap_binding": item.cap_binding,
                        "blockers": item.blockers,
                        "notes": item.notes,
                    }
                    for item in review.outcomes
                ],
            }
            for name, review in reviews.items()
        },
        "issued_reconciliation": {
            contract.contract: {
                **_jsonable(asdict(item)),
                "reopening": item.reopening,
            }
            for contract in bundle.contracts.values()
            for item in [reconcile_issued(bundle, contract, evaluate_contract(bundle, contract, catalog))]
            if item is not None
        },
    }
    print(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="price_index", description="采购价格指数与合同调价")
    parser.add_argument("--data", help="资料目录（默认为 data/）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("summary", help="各系列编制概览").set_defaults(func=cmd_summary)
    sub.add_parser("report", help="连续期间、区域与类别的完整报告").set_defaults(func=cmd_report)

    p_series = sub.add_parser("series", help="查看单个系列的逐期指数")
    p_series.add_argument("series")
    p_series.add_argument("--missing", action="store_true", help="列出未计入的篮子单元")
    p_series.set_defaults(func=cmd_series)

    p_weights = sub.add_parser("weights", help="查看基期篮子与支出权重")
    p_weights.add_argument("series")
    p_weights.set_defaults(func=cmd_weights)

    sub.add_parser("bridges", help="评估衔接记录").set_defaults(func=cmd_bridges)

    p_contracts = sub.add_parser("contracts", help="合同审签区间与补证")
    p_contracts.add_argument("--scenario", help="只看某个情景")
    p_contracts.set_defaults(func=cmd_contracts)

    sub.add_parser("revisions", help="复核情景对系列与合同的影响").set_defaults(func=cmd_revisions)

    p_trace = sub.add_parser("trace", help="逐单元追溯某期指数")
    p_trace.add_argument("series")
    p_trace.add_argument("period")
    p_trace.set_defaults(func=cmd_trace)

    p_comp = sub.add_parser("composition", help="固定篮子与朴素指标的差异")
    p_comp.add_argument("series")
    p_comp.add_argument("period")
    p_comp.set_defaults(func=cmd_composition)

    p_splice = sub.add_parser("splice", help="展示混接序列会误算基期差")
    p_splice.add_argument("--primary", default="IW")
    p_splice.add_argument("--fallback", default="IA")
    p_splice.add_argument("--period", default="2026-06")
    p_splice.set_defaults(func=cmd_splice)

    p_issued = sub.add_parser("issued", help="回查已审签单价与更正处理")
    p_issued.add_argument(
        "--correction",
        nargs=6,
        action="append",
        metavar=("合同", "序列", "月份", "原值", "新值", "登记日"),
        help="登记一条公示值更正；可重复",
    )
    p_issued.set_defaults(func=cmd_issued)

    sub.add_parser("json", help="输出结构化结果").set_defaults(func=cmd_json)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

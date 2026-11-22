"""指数编制、规格衔接与合同调价的业务规则回归测试。"""
import unittest
from decimal import Decimal

from price_index.contracts import (
    compile_catalog,
    evaluate_contract,
    reconcile_issued,
    review_contracts,
    revision_report,
    splicing_counterexample,
    standard_scenarios,
    Correction,
)
from price_index.index import (
    PriceIndex,
    basket_composition,
    bridge_assessments,
    compile_series,
    composition_comparison,
    spec_comparability,
)
from price_index.model import load_bundle

D = Decimal


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()

    def test_periods_are_contiguous_months(self):
        periods = self.bundle.periods
        self.assertEqual(periods[0], "2025-01")
        self.assertEqual(periods[-1], "2026-08")
        self.assertEqual(len(periods), 20)

    def test_supplier_presence_distinguishes_states(self):
        # A 在 north 全程在点；B 未在 north 开点。
        self.assertEqual(self.bundle.presence("2025-01", "north", "A"), "在点")
        self.assertEqual(self.bundle.presence("2025-01", "north", "B"), "尚未开点")
        # 2026-06 起 B 不再提供 bath_one，但网点仍在，属于停项而非停点。
        self.assertEqual(self.bundle.presence("2026-06", "west", "B"), "在点")


class ComparabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()

    def test_same_unit_different_components_requires_bridge(self):
        ok, reason = spec_comparability(
            self.bundle.specs["bath_one"], self.bundle.specs["bath_travel"]
        )
        self.assertTrue(ok)
        self.assertIn("衔接", reason)

    def test_package_visit_with_same_components_can_be_bridged(self):
        ok, _ = spec_comparability(
            self.bundle.specs["combo"], self.bundle.specs["bath_travel"]
        )
        self.assertTrue(ok)

    def test_different_units_and_components_not_comparable(self):
        ok, _ = spec_comparability(
            self.bundle.specs["combo"], self.bundle.specs["bath_two"]
        )
        self.assertFalse(ok)

    def test_different_categories_not_comparable(self):
        ok, _ = spec_comparability(
            self.bundle.specs["escort"], self.bundle.specs["bath_one"]
        )
        self.assertFalse(ok)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.assessments = {a.bridge.id: a for a in bridge_assessments(cls.bundle)}

    def test_only_evidence_supported_accepted_bridge_is_usable(self):
        usable = {key for key, item in self.assessments.items() if item.usable}
        self.assertEqual(usable, {"B1"})

    def test_accepted_but_contradicted_bridge_rejected(self):
        # B5 虽为 accepted，但整包与单项没有同一交易批次的可比证据。
        self.assertFalse(self.assessments["B5"].usable)
        self.assertIn("同一交易批次", self.assessments["B5"].reason)

    def test_filed_bridge_with_evidence_still_needs_status(self):
        item = self.assessments["B4"]
        self.assertTrue(item.evidence_ok)
        self.assertFalse(item.usable)
        self.assertIn("filed", item.reason)

    def test_withdrawn_bridge_not_used(self):
        self.assertFalse(self.assessments["B2"].usable)


class StatusTaxonomyTests(unittest.TestCase):
    """机构开点、停点、缺报、试行、优惠与缺少可比价格必须是不同情形。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.engine = PriceIndex.create(cls.bundle)

    def test_not_yet_open_is_distinct(self):
        resolution = self.engine.resolve("2025-01", "north", "B", "bath_one")
        self.assertEqual(resolution.status, "机构尚未开点")

    def test_upstream_missing_report(self):
        resolution = self.engine.resolve("2025-08", "west", "D", "bath_travel")
        self.assertEqual(resolution.status, "缺报")

    def test_trial_and_promotion_are_distinct(self):
        trial = self.engine.resolve("2026-06", "west", "D", "bath_travel")
        self.assertEqual(trial.status, "仅有试行价")
        promotion = self.engine.resolve("2025-11", "north", "A", "combo")
        self.assertEqual(promotion.status, "仅有优惠价")

    def test_item_stopped_without_comparable_bridge(self):
        # A 在 north 2026-08 仍在点，但停供 bath_one；B1 已过窗口，视为缺少可比价格。
        resolution = self.engine.resolve("2026-08", "north", "A", "bath_one")
        self.assertEqual(resolution.status, "本规格本期无记录")
        self.assertFalse(resolution.available)
        self.assertIn("停项", resolution.detail)


class BasketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()

    def test_base_basket_has_no_duplicate_trade_weight(self):
        # 基期所有 linked_group 都不含整包与拆分并存，故无需剔除。
        for series in ("IN", "IW", "IA", "IE"):
            composition = basket_composition(compile_series(self.bundle, series))
            self.assertEqual(composition["excluded"], [], series)

    def test_package_and_split_in_same_group_only_counted_once(self):
        # 2025-08 批次 8-A-G 同时登记 bath_one、travel 与整包 combo。
        # 以 2025-08 为基期时，整包必须被剔除，避免同一交易重复计权。
        from dataclasses import replace
        from price_index.index import build_basket

        definition = replace(self.bundle.series["IN"], base="2025-08")
        basket = build_basket(self.bundle, definition)
        specs_in_group = [
            cell.spec for cell in basket.cells if cell.linked_group == "8-A-G"
        ]
        self.assertIn("bath_one", specs_in_group)
        self.assertIn("travel", specs_in_group)
        self.assertNotIn("combo", specs_in_group)
        self.assertTrue(
            any(record_id == "8-A-combo" for _, record_id in basket.excluded)
        )

    def test_weights_are_base_expenditure(self):
        composition = basket_composition(compile_series(self.bundle, "IA"))
        self.assertEqual(composition["total_expenditure"], D("2920000"))
        self.assertAlmostEqual(
            float(composition["by_spec"]["bath_one"]["share"]), 0.437397, places=6
        )


class SeriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.catalog = compile_catalog(cls.bundle)

    def test_base_period_is_100(self):
        for name, definition in self.bundle.series.items():
            self.assertEqual(self.catalog[name].value(definition.base), D("100.0000"))

    def test_coverage_gate_marks_interruptions(self):
        iw = self.catalog["IW"]
        self.assertFalse(iw.published("2025-11"))
        self.assertFalse(iw.published("2026-01"))
        self.assertTrue(iw.published("2025-12"))
        # 中断月份的覆盖低于合同约定门槛。
        self.assertLess(iw.period("2025-11").coverage, iw.min_coverage)

    def test_bridge_fills_missing_quote_only_where_registered(self):
        # IN 2025-12 的 bath_one 缺报，由 B1 从 bath_travel 换算；北区在 B1 窗口内。
        result = self.catalog["IN"].period("2025-12")
        self.assertTrue(result.published)
        bridged = [
            trace
            for trace in result.traces
            if trace.resolution.status == "经衔接"
        ]
        self.assertTrue(bridged)
        self.assertTrue(all(trace.resolution.chain[0][0] == "B1" for trace in bridged))

    def test_bridge_outside_window_is_not_used(self):
        # B1 至 2026-07 结束；2026-08 不得再借 B1 换算。
        resolution = PriceIndex.create(self.bundle).resolve(
            "2026-08", "north", "A", "bath_one"
        )
        self.assertFalse(resolution.available)

    def test_promotion_and_trial_not_used_as_regular_prices(self):
        # 2025-11 的 combo 促销价不进入 IA 常规成交。
        resolution = PriceIndex.create(self.bundle).resolve(
            "2025-11", "north", "A", "combo"
        )
        self.assertEqual(resolution.status, "仅有优惠价")
        # 2026-06 的 bath_travel 仅为试行价，不作为常规可比价格。
        trial = PriceIndex.create(self.bundle).resolve(
            "2026-06", "west", "D", "bath_travel"
        )
        self.assertEqual(trial.status, "仅有试行价")


class CompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()

    def test_fixed_basket_differs_from_unit_value(self):
        # 单位值会把高价规格占比变化算成价格变化，固定篮子不应如此。
        comparison = composition_comparison(self.bundle, "IN", "2026-06")
        self.assertIsNotNone(comparison.fixed_basket)
        self.assertNotEqual(comparison.fixed_basket, comparison.unit_value)
        self.assertGreater(comparison.simple_average, comparison.fixed_basket)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.catalog = compile_catalog(cls.bundle)
        cls.reviews, cls.scenarios = review_contracts(cls.bundle)

    def test_fallback_used_whole_series_not_spliced(self):
        evaluation = self.reviews["C01"].baseline
        self.assertTrue(evaluation.fallback_used)
        self.assertEqual(evaluation.series_used, "IA")
        self.assertEqual(evaluation.base.series, "IA")
        self.assertEqual(evaluation.end.series, "IA")

    def test_primary_series_without_fallback_is_suspended(self):
        review = self.reviews["C02"]
        self.assertFalse(review.baseline.signable)
        self.assertEqual(review.baseline.blockers[0]["code"], "no_fallback")
        self.assertTrue(review.blocking_evidence())

    def test_cap_clamps_ratio(self):
        review = self.reviews["C04"]
        evaluation = next(
            item for item in review.outcomes if item.scenario == "missing_carry"
        )
        self.assertTrue(evaluation.signable)
        self.assertEqual(evaluation.ratio_applied, evaluation.upper_ratio)
        self.assertEqual(evaluation.cap_binding, "upper")

    def test_sensitivity_does_not_enter_signable_interval(self):
        review = self.reviews["C02"]
        # 敏感性口径能让 C02 可审签，但不得进入正式区间。
        sensitivity = [
            item for item in review.outcomes if item.scenario == "missing_carry"
        ][0]
        self.assertTrue(sensitivity.signable)
        self.assertNotIn("missing_carry", review.admissible_names)
        self.assertNotIn(
            sensitivity.amount,
            [review.amount_interval[0], review.amount_interval[1]]
            if review.amount_interval
            else [],
        )

    def test_contingent_bridge_renewal_is_treated_separately(self):
        review = self.reviews["C02"]
        contingent = {
            item.scenario for item in review.contingent_outcomes if item.signable
        }
        self.assertIn("renew_B2_through_2026_08", contingent)
        sensitivity = {item.scenario for item in review.sensitivity_outcomes}
        self.assertNotIn("renew_B2_through_2026_08", sensitivity)

    def test_fallback_endpoints_both_required(self):
        # C03 主序列 IN 两端均公示，不应转用 IA。
        evaluation = self.reviews["C03"].baseline
        self.assertFalse(evaluation.fallback_used)
        self.assertEqual(evaluation.series_used, "IN")

    def test_fallback_with_one_endpoint_unpublished_is_blocked(self):
        from dataclasses import replace

        # 主序列 IE 在 2026-01 与 2026-02 中断；备选 IW 也只在 2026-02 公示。
        contract = replace(
            self.bundle.contracts["C05"],
            series="IE",
            fallback="IW",
            first_period="2026-01",
            last_period="2026-02",
        )
        evaluation = evaluate_contract(self.bundle, contract, self.catalog)
        self.assertFalse(evaluation.signable)
        self.assertEqual(
            evaluation.blockers[0]["code"], "primary_and_fallback_incomplete"
        )
        self.assertFalse(evaluation.fallback_used)


class RevisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.scenarios = {s.name: s for s in standard_scenarios(cls.bundle)}

    def test_bridge_renewal_restores_some_but_not_all_contracts(self):
        report = revision_report(self.bundle, self.scenarios["renew_B2_through_2026_08"])
        restored = {(c.series, c.period) for c in report.publication_restorations}
        self.assertIn(("IW", "2026-06"), restored)
        self.assertIn(("IA", "2026-08"), restored)
        affected = set(report.affected_contracts)
        # 恢复公示影响 C02（待审签），但不改变 C01 的可执行调价来源。
        self.assertIn("C02", affected)
        self.assertNotIn("C04", affected)

    def test_no_bridge_scenario_changes_values_not_publication(self):
        report = revision_report(self.bundle, self.scenarios["no_bridges"])
        self.assertTrue(report.value_changes)
        self.assertFalse(report.publication_restorations)


class SplicingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()

    def test_splicing_shows_base_difference_is_not_price_change(self):
        result = splicing_counterexample(self.bundle)
        self.assertTrue(result["ok"])
        # 混接比价与同序列比价方向相反，说明基期差被误算。
        self.assertLess(result["naive_spliced_ratio"], D(1))
        self.assertGreater(result["true_same_series_ratio"], D(1))
        self.assertLess(result["base_difference"], D(0))


class IssuedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle()
        cls.catalog = compile_catalog(cls.bundle)

    def _reconcile(self, name, corrections=None):
        contract = self.bundle.contracts[name]
        current = evaluate_contract(self.bundle, contract, self.catalog)
        return reconcile_issued(self.bundle, contract, current, corrections)

    def test_recomputation_alone_does_not_reopen(self):
        result = self._reconcile("C01")
        self.assertFalse(result.published_value_changed)
        self.assertIn("不重开", result.reopening)

    def test_no_reopen_rule_respected(self):
        result = self._reconcile("C04")
        self.assertIn("no_reopen", result.reopening)

    def test_recorded_amount_matches_contract_terms(self):
        result = self._reconcile("C01")
        self.assertTrue(result.recorded_consistent)
        self.assertEqual(result.recorded_amount, D("18812"))

    def test_registered_correction_forms_difference(self):
        correction = Correction(
            series="IA",
            period="2025-10",
            old_value=D("104.2"),
            new_value=D("102.0"),
            recorded_on="2026-08-01",
        )
        result = self._reconcile("C01", [correction])
        self.assertTrue(result.published_value_changed)
        self.assertTrue(result.executable_changed)
        self.assertIn("差额", result.reopening)
        self.assertEqual(result.corrected_amount, D("19080"))

    def test_correction_with_wrong_old_value_is_flagged(self):
        correction = Correction(
            series="IA",
            period="2025-10",
            old_value=D("999"),
            new_value=D("102.0"),
            recorded_on="2026-08-01",
        )
        result = self._reconcile("C01", [correction])
        self.assertTrue(result.correction_mismatch)
        self.assertIn("核对", result.reopening)


if __name__ == "__main__":
    unittest.main()

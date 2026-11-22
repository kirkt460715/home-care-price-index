"""装载采购价格指数所需的各集合，并建立可追溯的索引。

所有金额、数量和系数一律使用 ``Decimal``，避免二进制浮点在指数与调价金额上
产生末位差异。空串表示该字段在该状态下没有取值，装载时不把空串当作 0。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from inspect_records import DATA, read_collection

PERIOD_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

PRICE_STATES = {"regular", "missing", "trial", "promotion"}
BRIDGE_STATES = {"filed", "accepted", "withdrawn"}
LATE_RULES = {"correction", "no_reopen"}


class DataError(ValueError):
    """交换资料不满足编译所需的业务约束。"""


def to_decimal(value: str, where: str) -> Decimal:
    """把登记值转换为 Decimal；空串或非数值直接报错。"""
    if value == "":
        raise DataError(f"{where} 缺少数值")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise DataError(f"{where} 的数值无效：{value!r}") from error


def to_period(value: str, where: str) -> str:
    """校验并返回 YYYY-MM 形式的月份。"""
    import re

    if not re.fullmatch(PERIOD_PATTERN, value):
        raise DataError(f"{where} 的月份格式无效：{value!r}")
    return value


@dataclass(frozen=True)
class Spec:
    spec: str
    category: str
    unit: str
    components: frozenset[str]
    raw_components: str

    @property
    def is_package(self) -> bool:
        return self.unit == "package"


@dataclass(frozen=True)
class Observation:
    id: str
    period: str
    supplier: str
    area: str
    spec: str
    price: Decimal | None
    quantity: Decimal | None
    state: str
    linked_group: str

    @property
    def priced(self) -> bool:
        return self.state == "regular" and self.price is not None


@dataclass(frozen=True)
class Bridge:
    id: str
    left: str
    right: str
    valid_area: str
    first_period: str
    last_period: str
    rate: Decimal
    state: str

    def covers(self, area: str, period: str) -> bool:
        area_ok = self.valid_area in (area, "*")
        return area_ok and self.first_period <= period <= self.last_period


@dataclass(frozen=True)
class SeriesDef:
    series: str
    area: str
    category: str
    base: str
    min_coverage: Decimal


@dataclass(frozen=True)
class Contract:
    contract: str
    series: str
    fallback: str
    first_period: str
    last_period: str
    reference_price: Decimal
    max_change: Decimal
    late_rule: str
    state: str

    @property
    def cap_ratio(self) -> Decimal:
        return Decimal(1) + self.max_change


@dataclass(frozen=True)
class IssuedPrice:
    contract: str
    series: str
    base_observed: Decimal
    end_observed: Decimal
    amount: Decimal
    issued_on: str


@dataclass
class Bundle:
    """一次装载得到的只读资料集合及其检索索引。"""

    specs: dict[str, Spec]
    observations: list[Observation]
    bridges: list[Bridge]
    series: dict[str, SeriesDef]
    contracts: dict[str, Contract]
    issued: dict[str, IssuedPrice]
    periods: list[str]
    _by_cell: dict[tuple[str, str, str, str], Observation] = field(
        default_factory=dict, repr=False
    )
    _by_period: dict[str, list[Observation]] = field(default_factory=dict, repr=False)
    _supplier_periods: dict[tuple[str, str], list[str]] = field(
        default_factory=dict, repr=False
    )

    def observation(
        self, period: str, area: str, supplier: str, spec: str
    ) -> Observation | None:
        return self._by_cell.get((period, area, supplier, spec))

    def supplier_periods(self, area: str, supplier: str) -> list[str]:
        """该机构在该区域有记录（含缺报）的月份，按时间排序。"""
        return self._supplier_periods.get((area, supplier), [])

    def presence(self, period: str, area: str, supplier: str) -> str:
        """判断机构在该区域该月的存在形态，区分开点、停点、临时未报与在点。"""
        periods = self.supplier_periods(area, supplier)
        if not periods:
            return "尚未开点"
        if period < periods[0]:
            return "尚未开点"
        if period > periods[-1]:
            return "已停点"
        if period in periods:
            return "在点"
        return "临时未报"

    def period_rows(self, period: str) -> list[Observation]:
        return self._by_period.get(period, [])

    def observations_between(self, first: str, last: str) -> list[Observation]:
        return [
            row
            for row in self.observations
            if first <= row.period <= last
        ]


def _load_specs(rows: list[dict]) -> dict[str, Spec]:
    specs: dict[str, Spec] = {}
    for row in rows:
        components = frozenset(
            part.strip() for part in row["components"].split(",") if part.strip()
        )
        if not row["category"] or not row["unit"]:
            raise DataError(f"规格 {row['spec']} 缺少类别或计量单位")
        specs[row["spec"]] = Spec(
            spec=row["spec"],
            category=row["category"],
            unit=row["unit"],
            components=components,
            raw_components=row["components"],
        )
    return specs


def _load_observations(rows: list[dict], specs: dict[str, Spec]) -> list[Observation]:
    observations: list[Observation] = []
    seen_cells: set[tuple[str, str, str, str]] = set()
    for row in rows:
        where = f"prices/{row['id']}"
        period = to_period(row["period"], where)
        if row["spec"] not in specs:
            raise DataError(f"{where} 引用了未登记的规格 {row['spec']}")
        if row["state"] not in PRICE_STATES:
            raise DataError(f"{where} 的状态无效：{row['state']}")
        if row["state"] == "missing":
            if row["unit_price"] or row["quantity"]:
                raise DataError(f"{where} 为缺报却登记了价格或数量")
            price = quantity = None
        else:
            price = to_decimal(row["unit_price"], f"{where}.unit_price")
            quantity = to_decimal(row["quantity"], f"{where}.quantity")
            if price <= 0:
                raise DataError(f"{where} 的成交价必须为正")
            if quantity < 0:
                raise DataError(f"{where} 的交易量不能为负")
        cell = (period, row["area"], row["supplier"], row["spec"])
        if cell in seen_cells:
            raise DataError(f"{where} 与同月同机构同规格的记录重复")
        seen_cells.add(cell)
        observations.append(
            Observation(
                id=row["id"],
                period=period,
                supplier=row["supplier"],
                area=row["area"],
                spec=row["spec"],
                price=price,
                quantity=quantity,
                state=row["state"],
                linked_group=row["linked_group"],
            )
        )
    observations.sort(key=lambda item: (item.period, item.area, item.supplier, item.spec))
    return observations


def _load_bridges(rows: list[dict], specs: dict[str, Spec]) -> list[Bridge]:
    bridges: list[Bridge] = []
    for row in rows:
        where = f"bridges/{row['id']}"
        if row["left"] not in specs or row["right"] not in specs:
            raise DataError(f"{where} 引用了未登记的规格")
        if row["state"] not in BRIDGE_STATES:
            raise DataError(f"{where} 的状态无效：{row['state']}")
        first = to_period(row["first_period"], where)
        last = to_period(row["last_period"], where)
        if first > last:
            raise DataError(f"{where} 的生效区间首尾颠倒")
        rate = to_decimal(row["rate"], f"{where}.rate")
        if rate <= 0:
            raise DataError(f"{where} 的换算系数必须为正")
        bridges.append(
            Bridge(
                id=row["id"],
                left=row["left"],
                right=row["right"],
                valid_area=row["valid_area"],
                first_period=first,
                last_period=last,
                rate=rate,
                state=row["state"],
            )
        )
    bridges.sort(key=lambda item: item.id)
    return bridges


def _load_series(rows: list[dict], specs: dict[str, Spec]) -> dict[str, SeriesDef]:
    series: dict[str, SeriesDef] = {}
    known_categories = {spec.category for spec in specs.values()}
    for row in rows:
        where = f"series/{row['series']}"
        if row["category"] not in known_categories:
            raise DataError(f"{where} 的类别未在规格中出现：{row['category']}")
        min_coverage = to_decimal(row["min_coverage"], f"{where}.min_coverage")
        if not (0 < min_coverage <= 1):
            raise DataError(f"{where} 的最低覆盖必须在 (0, 1] 内")
        series[row["series"]] = SeriesDef(
            series=row["series"],
            area=row["area"],
            category=row["category"],
            base=to_period(row["base"], where),
            min_coverage=min_coverage,
        )
    return series


def _load_contracts(rows: list[dict], series: dict[str, SeriesDef]) -> dict[str, Contract]:
    contracts: dict[str, Contract] = {}
    for row in rows:
        where = f"contracts/{row['contract']}"
        if row["series"] not in series:
            raise DataError(f"{where} 引用了未登记的系列 {row['series']}")
        if row["fallback"] and row["fallback"] not in series:
            raise DataError(f"{where} 引用了未登记的备选系列 {row['fallback']}")
        if row["fallback"] == row["series"]:
            raise DataError(f"{where} 的备选系列不能与主系列相同")
        first = to_period(row["first_period"], where)
        last = to_period(row["last_period"], where)
        if first > last:
            raise DataError(f"{where} 的窗口首尾颠倒")
        if row["late_rule"] not in LATE_RULES:
            raise DataError(f"{where} 的更正规则无效：{row['late_rule']}")
        reference = to_decimal(row["reference_price"], f"{where}.reference_price")
        max_change = to_decimal(row["max_change"], f"{where}.max_change")
        if reference <= 0:
            raise DataError(f"{where} 的参考单价必须为正")
        if not (0 <= max_change < 1):
            raise DataError(f"{where} 的调价限幅必须在 [0, 1) 内")
        contracts[row["contract"]] = Contract(
            contract=row["contract"],
            series=row["series"],
            fallback=row["fallback"],
            first_period=first,
            last_period=last,
            reference_price=reference,
            max_change=max_change,
            late_rule=row["late_rule"],
            state=row["state"],
        )
    return contracts


def _load_issued(rows: list[dict], contracts: dict[str, Contract]) -> dict[str, IssuedPrice]:
    issued: dict[str, IssuedPrice] = {}
    for row in rows:
        where = f"issued_prices/{row['contract']}"
        if row["contract"] not in contracts:
            raise DataError(f"{where} 引用了未登记的合同")
        base = to_decimal(row["base_observed"], f"{where}.base_observed")
        end = to_decimal(row["end_observed"], f"{where}.end_observed")
        amount = to_decimal(row["amount"], f"{where}.amount")
        if base <= 0 or end <= 0 or amount <= 0:
            raise DataError(f"{where} 的观测值或金额必须为正")
        issued[row["contract"]] = IssuedPrice(
            contract=row["contract"],
            series=row["series"],
            base_observed=base,
            end_observed=end,
            amount=amount,
            issued_on=row["issued_on"],
        )
    return issued


def load_bundle(directory: Path | str = DATA) -> Bundle:
    """读取全部集合并完成跨表校验。"""
    directory = Path(directory)
    names = ["specifications", "prices", "bridges", "series", "contracts", "issued_prices"]
    tables = {name: read_collection(name, directory)[0] for name in names}

    specs = _load_specs(tables["specifications"])
    observations = _load_observations(tables["prices"], specs)
    bridges = _load_bridges(tables["bridges"], specs)
    series = _load_series(tables["series"], specs)
    contracts = _load_contracts(tables["contracts"], series)
    issued = _load_issued(tables["issued_prices"], contracts)

    by_cell = {
        (row.period, row.area, row.supplier, row.spec): row for row in observations
    }
    by_period: dict[str, list[Observation]] = defaultdict(list)
    for row in observations:
        by_period[row.period].append(row)
    supplier_periods: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in observations:
        supplier_periods[(row.area, row.supplier)].append(row.period)
    supplier_periods = {
        key: sorted(set(periods)) for key, periods in supplier_periods.items()
    }
    periods = sorted(by_period)
    for definition in series.values():
        if definition.base not in periods:
            raise DataError(f"系列 {definition.series} 的基期 {definition.base} 没有记录")

    return Bundle(
        specs=specs,
        observations=observations,
        bridges=bridges,
        series=series,
        contracts=contracts,
        issued=issued,
        periods=periods,
        _by_cell=by_cell,
        _by_period=dict(by_period),
        _supplier_periods=supplier_periods,
    )

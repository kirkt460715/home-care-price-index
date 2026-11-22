# 资料说明

各集合保存在 `data/`，JSON 对象的 `fields` 为字段列表，`key` 为集合主键，`rows` 为记录数组。各记录值均为字符串，空串含义以各集合的说明为准。

## specifications

规格名称、履约单位、包括项目和地域来源。

主键为 `spec`，字段为 `spec`、`category`、`unit`、`components`。

## prices

实际交易量和单位成交价。missing 表示收到缺报通知，无成交证据。

主键为 `id`，字段为 `id`、`period`、`supplier`、`area`、`spec`、`unit_price`、`quantity`、`state`、`linked_group`。

## bridges

规格衔接记录，rate 是左规格价格换算到右规格的系数。valid_area 是局部授权，* 是全域。state 为 filed、accepted 或 withdrawn。

主键为 `id`，字段为 `id`、`left`、`right`、`valid_area`、`first_period`、`last_period`、`rate`、`state`。

## series

出数系列的约定范围及基期，最低覆盖为能衔接的基期支出份额。

主键为 `series`，字段为 `series`、`area`、`category`、`base`、`min_coverage`。

## contracts

合同调价条款，窗口首尾为对应序列的月份。fallback 为空表示不允许转用。late_rule 为 correction 或 no_reopen。

主键为 `contract`，字段为 `contract`、`series`、`fallback`、`first_period`、`last_period`、`reference_price`、`max_change`、`late_rule`、`state`。

## issued_prices

已生效早期结果及当时真实采用的系列。

主键为 `contract`，字段为 `contract`、`series`、`base_observed`、`end_observed`、`amount`、`issued_on`。

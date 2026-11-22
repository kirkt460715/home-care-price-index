# 资料说明

各集合保存在 `data/`，JSON 对象的 `fields` 为字段列表，`key` 为集合主键，`rows` 为记录数组。各记录值均为字符串，空串含义以各集合的说明为准。

## specifications

规格名称、履约单位、包括项目和地域来源。

主键为 `spec`，字段为 `spec`、`category`、`unit`、`components`。

## prices

实际交易量和单位成交价。missing 表示收到缺报通知，无成交证据。

主键为 `id`，字段为 `id`、`period`、`supplier`、`area`、`spec`、`unit_price`、`quantity`、`state`、`linked_group`。

`state` 的取值与处理：

| 取值 | 含义 | 指数处理 |
| --- | --- | --- |
| `regular` | 常规成交 | 进入常规价格 |
| `missing` | 收到缺报通知，无成交证据 | 不计价，`unit_price` 与 `quantity` 必须为空 |
| `trial` | 试行价 | 不作为常规价格 |
| `promotion` | 优惠价 | 不作为常规价格 |

机构在某区域某月的存在形态由该机构在该区域的记录区间推断，用于区分
“尚未开点”“已停点”“临时未报”与“在点但停项”，避免把不同情形都当作同一类
缺少可比价格。`linked_group` 相同的记录可能引用同批交易；同一批次内若同时
登记整包与拆分规格，编制时只保留一种表示，防止同一交易量重复计权。

## bridges

规格衔接记录，rate 是左规格价格换算到右规格的系数（右规格价格 = 左规格价格 ×
rate）。valid_area 是局部授权，* 是全域。state 为 filed、accepted 或 withdrawn。

主键为 `id`，字段为 `id`、`left`、`right`、`valid_area`、`first_period`、`last_period`、`rate`、`state`。

编制时 `accepted` 只是必要条件，还须与重叠期实际成交比价相符；整包与单项之间
还须能对应到同一 `linked_group` 的成交。详见[编制方法](methodology.md)。

## series

出数系列的约定范围及基期，最低覆盖为能衔接的基期支出份额。

主键为 `series`，字段为 `series`、`area`、`category`、`base`、`min_coverage`。

`area` 为 `*` 时表示跨区域全域序列。基期当月必须存在记录。已覆盖的基期支出
份额低于 `min_coverage` 时，该期不公示。

## contracts

合同调价条款，窗口首尾为对应序列的月份。fallback 为空表示不允许转用。late_rule 为 correction 或 no_reopen。

主键为 `contract`，字段为 `contract`、`series`、`fallback`、`first_period`、`last_period`、`reference_price`、`max_change`、`late_rule`、`state`。

`state` 为 `issued` 时表示已审签并开始适用；`pending` 表示等待计算。`max_change`
是双向调价限幅比例。`fallback` 只在主序列端点未公示时使用，且必须在窗口两端
都公示，不得与主序列混接。

## issued_prices

已生效早期结果及当时真实采用的系列。

主键为 `contract`，字段为 `contract`、`series`、`base_observed`、`end_observed`、`amount`、`issued_on`。

回查时先核对 `amount` 是否由 `reference_price`、`base_observed`、`end_observed`
与合同限幅自洽；后续重算与记录的差异只有存在正式公示值更正记录时才形成差额
处理。

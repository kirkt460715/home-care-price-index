# 采购价格指数的编制与合同调价复核

本目录把 `data/` 中的合同规格、报价与实际交易量编制为可重复的采购价格指数，
并据此复核合同调价。命令只依赖标准库，全部金额以分为单位、以 `Decimal` 计算，
同一份资料与同一口径得到的结果逐位一致。

```sh
python3 -m price_index report          # 完整报告：权重、指数、断点、审签、敏感性
python3 -m price_index summary         # 各系列编制概览
python3 -m price_index series IN       # 逐期指数与覆盖
python3 -m price_index weights IA      # 基期篮子与支出权重
python3 -m price_index bridges         # 衔接记录的证据评估
python3 -m price_index contracts       # 合同审签区间、补证与暂缓
python3 -m price_index revisions       # 复核情景对系列与合同的影响
python3 -m price_index trace IN 2026-06 # 逐单元追溯某期指数
python3 -m price_index composition IN 2026-06 # 固定篮子与朴素指标对照
python3 -m price_index splice          # 演示混接序列为何会误算基期差
python3 -m price_index issued          # 回查已审签单价与更正处理
python3 -m price_index json > result.json
```

## 一、比较单位：规格、计量单位与附加项

`specifications` 用三个维度定义比较单位：

| 维度 | 例子 | 含义 |
| --- | --- | --- |
| `category` | `bath`、`escort` | 服务类别 |
| `unit` | `visit`、`hour`、`trip`、`package` | 计量单位 |
| `components` | `single_worker`、`two_workers`、`travel` | 约定包含的附加项 |

`spec_comparability` 据此判断两个规格能否替代：

* 计量单位相同、包括项目相同：直接可比。
* 计量单位相同、包括项目不同（例如 `bath_one` 对 `bath_travel`）：结构上可
  换算，但**必须**依赖已登记且有证据的衔接。
* 整包对单项（`package` 对 `visit`）：只有包括项目完全一致时才可能换算，且
  同样必须有衔接与同一交易批次的证据。
* 类别不同或单位无法对应：不可比。

这正是采购会上“同一家机构助浴价格没变”的症结：`bath_one` 与
`bath_travel` 单价口径不同，把含交通的新合同单价直接与旧单价相比并不是同
一规格的价格变动，必须经衔接换算或改用可比规格的固定篮子指数。

## 二、指数口径

* **基期篮子**：基期当月、同一区域与类别、状态为 `regular` 的成交记录。
* **权数**：基期支出（`unit_price × quantity`）。覆盖率同样按基期支出份额衡量，
  与权数口径无关。
* **逐期取价**：每个篮子单元优先取本期同规格 `regular` 成交；没有时，只有在
  已登记、状态 `accepted`、覆盖该区域与月份，并且换算系数与重叠期实际成交
  比价相符时，才允许经衔接取得。整包与单项还必须对应同一 `linked_group`。
* **覆盖率门槛**：已覆盖单元的基期支出份额低于 `series.min_coverage` 时，该期
  **不公示**，不使用相邻月份或另一序列补齐。
* **公示位数**：指数保留 4 位小数，比例保留 6 位小数，金额四舍五入到分。

固定篮子指数的定义使高价规格占比变化不会自动抬高指数。`composition` 命令给出
对照：同一批可比单元上，单位值指数与简单算术平均会随规格结构变化而偏离，不能
用于合同调价。

## 三、规格替代的证据规则

`bridges` 的 `rate` 是“左规格价格 × rate = 右规格价格”。可用性按三层判断：

1. **结构可比**：按第一节的类别、单位与包括项目判断。
2. **状态有效**：只采用 `policy.bridge_states` 中的状态（基线为 `accepted`）。
3. **证据相符**：在衔接窗口内，用同机构、同区域、同月份的两种规格成交价计算
   实际比价，与登记系数比较。偏差超过 `evidence_tolerance`（基线 10%）即视为
   证据不足。整包与单项之间没有同一交易批次（`linked_group`）的对应成交时，
   即使状态为 `accepted` 也不得使用。

用当前资料运行的结果：

| 衔接 | 状态 | 判定 | 原因 |
| --- | --- | --- | --- |
| B1 `bath_one→bath_travel` | accepted | 可用 | 重叠比价 1.2624，与 1.30 偏差 2.98% |
| B2 `bath_one→bath_travel`（west） | withdrawn | 不采用 | 状态已撤回 |
| B3 `combo→bath_travel` | filed | 不采用 | 未生效；且整包无同批次证据 |
| B4 `bath_two→bath_travel` | filed | 不采用 | 有证据但未生效，需先审定 |
| B5 `combo→bath_travel` | accepted | 不采用 | 整包与单项无同批次对应成交 |

`revisions` 中的 `accept_filed_with_evidence` 与 `reinstate_B2` 情景展示：把这些
衔接补登为 `accepted` 后，哪些区域重新具备公示条件。`renew_B2_through_2026_08`
进一步续登窗口，能同时恢复 IW 2026-06～08 与 IA 2026-08 的公示，但只影响仍开放
调价的合同（例如 C02），对已审签的 C04 不产生可执行调价变化。

## 四、合同调价与审签区间

`evaluate_contract` 的步骤：

1. 取合同窗口首月与末月在**同一序列**上的已公示指数。
2. 主序列任一端未公示时：
   * 有 `fallback` 且 fallback 在**两端都公示**：整体转用 fallback；
   * 否则暂缓，列出必要补证，绝不混接两条序列。
3. 调价比例 = 末值 / 首值，按 `max_change` 双向限幅。
4. 金额 = `reference_price × 限幅后比例`，四舍五入到分。

`contracts` 命令输出的“可审签区间”取 `baseline` 与 `contingent`（补证后成立）
情景的金额范围，并单列其中“补证后才成立”的部分；纯 `sensitivity` 情景只作参考，
不进入区间。每个合同同时给出必要补证与暂缓条款。

## 五、已审签结果的回查

`issued_prices` 记录当时真实采用的序列、观测值与金额。`issued` 命令：

* 先核对记录金额是否与合同条款自洽（`reference_price × 限幅比例`）。
* 区分三种变化：`published_value_changed`（公示值更正）、`source_changed`
  （序列来源变化）、`executable_changed`（实际可执行调价变化）。
* 只有**正式登记的公示值更正**才形成新的差额处理；仅因重算口径不同或候选指标
  更全面而产生的差异，不重开已定价合同。`no_reopen` 条款优先。
* 更正记录若与原记录版本不符（`old_value` 对不上），先核对版本，不直接调整。

## 六、数据状态的区分

`prices.state` 与机构存在形态分开处理：

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `regular` | 常规成交 | 进入指数 |
| `missing` | 收到缺报通知 | 不计价；列明缺报单元 |
| `trial` | 试行价 | 不作为常规价格 |
| `promotion` | 优惠价 | 不作为常规价格 |
| 尚未开点 | 机构在该区域尚无记录 | 与缺报、停点区分 |
| 已停点 | 机构在该区域已有记录但此后无记录 | 与临时未报区分 |
| 临时未报 | 网点在点但该月整月无记录 | 不是停点 |
| 停项 | 网点在点但未提供某规格 | 不是停点 |

## 七、可追溯性

`trace` 对每一期、每个篮子单元列出：基期记录号与基期价、权数与权重、当期可比
价格及其来源记录号、状态，以及所用衔接的每一步（编号、方向、系数）。`weights`
列出基期支出在区域、机构、规格上的分布，以及因同一交易重复表示而剔除的记录。
`json` 输出包含同样的明细，可供进一步核对。

## 八、敏感性口径

`revisions` 内置以下对照，分三类：

| 情景 | 类型 | 用途 |
| --- | --- | --- |
| `baseline` | 正式 | 今天采用的报告口径 |
| `missing_carry` | 敏感性 | 缺报沿用前价会恢复哪些月份 |
| `quantity_weight` | 敏感性 | 交易量权数与支出权数的差异 |
| `package_weighted` | 敏感性 | 整包与拆分表示的选择 |
| `accept_filed_with_evidence` | 补证后 | 把 B4 审定为 accepted 后 |
| `reinstate_B2` | 补证后 | 恢复 B2 后 |
| `renew_B2_through_2026_08` | 补证后 | B2 恢复并续登后 |
| `coverage_70pct` | 敏感性 | 断点对门槛的敏感程度 |
| `no_bridges` | 敏感性 | 完全不使用衔接 |

“补证后”情景只是在完成衔接审定或恢复登记之后才可能成立的 prospectively 结果，
不是今天即可审签；报告在暂缓合同下单独列出，并在区间中标注“其中补证后才成立”。
结论对规格处理的敏感程度以合同金额区间呈现：正式与补证后情景给出可审签区间，
敏感性情景单列范围，两者不混用。

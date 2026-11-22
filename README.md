# 居家服务采购价格指数的规格衔接与合同调价

居家服务采购研究资料与编制工具。金额以分为单位，月份按北京时间。`spec` 可区分
计量单位与是否含交通；`linked_group` 相同的记录可能引用同批交易，按凭证引用
确定合计使用的权重；系列已公示的基准指数为其基期的 100；合同调价是采购单价
调整，记录中的调价限幅是比例。

资料均为合成记录，采用 UTF-8 编码。交换字段、标识和状态见[资料说明](docs/data.md)，
编制口径、证据规则与合同处理见[编制方法](docs/methodology.md)。

## 快速开始

项目使用 Python 3.11.2，只依赖标准库。

```sh
python3 inspect_records.py                 # 查看各集合记录数并检查交换结构
python3 -m unittest discover -s tests -v   # 业务规则与口径回归测试
python3 -m price_index report              # 完整报告
```

`price_index` 的主要子命令：

| 命令 | 用途 |
| --- | --- |
| `report` | 按连续期间、区域与类别输出权重、指数、断点、审签与敏感性 |
| `summary` | 各系列已公示与中断月份 |
| `series <系列> [--missing]` | 单系列逐期指数与未计入单元 |
| `weights <系列>` | 基期篮子与支出权重、重复计权剔除 |
| `bridges` | 衔接记录的结构、状态与证据评估 |
| `contracts` | 合同可审签区间、必要补证与暂缓条款 |
| `revisions` | 复核情景对系列公示与合同金额的影响 |
| `trace <系列> <月份>` | 逐单元追溯某期指数的价格、权数与衔接链 |
| `composition <系列> <月份>` | 固定篮子与单位值、简单平均的差异 |
| `splice` | 演示混接两条序列会把基期差误当涨价 |
| `issued [--correction …]` | 回查已审签单价与更正处理 |
| `json` | 结构化输出全部结果 |

## 目录

```
data/                 业务资料（specifications、prices、bridges、series、contracts、issued_prices）
docs/data.md          交换字段与状态说明
docs/methodology.md   指数编制、规格替代证据、合同调价与回查口径
price_index/          编制工具
  model.py            装载、校验与索引
  index.py            可比性、衔接评估、篮子、指数编制
  contracts.py        合同求值、情景、修订影响、已审签回查
  __main__.py         命令行入口
tests/                交换结构与业务规则测试
```

## 关键口径

* 固定基期篮子、基期支出加权；覆盖率按基期支出份额，低于系列门槛即中断公示。
* 规格替代只采用已登记、状态 `accepted`、且与重叠成交比价相符的衔接；整包与
  单项还必须对应同一交易批次。
* 主序列端点未公示时按合同 `fallback` **整体**转用备选序列，绝不混接基期。
* 已审签单价以当时实际采用的指数与合同条款为准；只有正式登记的公示值更正才
  形成新的差额处理。

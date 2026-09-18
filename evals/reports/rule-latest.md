# TideShift 对话/解释层评测报告

- 模式：**rule**（规则路由，离线确定性）
- 调度日 2024-07-30｜求解器状态 `Final_DR_Adjusted`｜XGBoost 关｜MILP 重跑 打桩
- 代码 `8c25aab`｜Python 3.13.9｜生成于 2026-09-18 23:40:00
- 用例 32 条，跳过 3 条，执行异常 0 条

## 指标

| 指标 | 值 | 样本 | 含义 |
|---|---|---|---|
| `task_success_rate` | 0.969 | 32 | 四维全对的用例占比（主指标） |
| `tool_call_accuracy` | 0.969 | 32 | 选对工具（含参数匹配）的比例 |
| `answer_completeness` | 1.000 | 32 | 回答要点齐全的比例 |
| `number_fidelity` | 1.000 | 9 | 数字与真实报表一致的比例 |
| `side_effect_guard_rate` | 1.000 | 5 | 不该重跑 MILP 时确实没重跑 |
| `grounding_catch_rate` | 0.750 | 4 | 编造数字被守卫拦下的比例 |
| `grounding_false_positive_rate` | 0.000 | 3 | 真数字被误报的比例（越低越好） |

- 延迟 p50 / p95：0.1 ms / 1.3 ms
- token：prompt 0 / completion 0（规则模式恒为 0，不参与结论）

## 按用例类型

| 类型 | 用例数 | 任务成功率 |
|---|---|---|
| act | 7 | 1.000 |
| explain | 5 | 1.000 |
| guard | 5 | 1.000 |
| query | 15 | 0.933 |

## 失败用例（1 条）

| 用例 | 类型 | 问题 | 实际调用 | 失败原因 |
|---|---|---|---|---|
| `q-baseline-temp` | query | 跟不考虑热的策略比，温度差多少 | get_thermal_info | `未调用期望工具 ['compare_baseline']，实际调用 ['get_thermal_info']` |

## 防幻觉守卫探针

| 探针 | 埋入的编造数字 | 被拦 | 漏网 | 误报 |
|---|---|---|---|---|
| `gp-fabricated-money` | ['4521', '0.87'] | ['4521.00 元', '0.87 元/kWh'] | — | — |
| `gp-fabricated-temp` | ['71.5'] | ['71.5 ℃'] | — | — |
| `gp-rounding-ok` | — | — | — | — |
| `gp-count-words` | — | — | — | — |
| `gp-unit-boundary` | ['0.9'] | — | ['0.9'] | — |
| `gp-clean-report` | — | — | — | — |

## 跳过（模式不适用，不计入分母）

- `e-digest-qa`：用例限定 ['llm']，当前模式 rule
- `l-multi-tool`：用例限定 ['llm']，当前模式 rule
- `l-what-if`：用例限定 ['llm']，当前模式 rule

## 与基线比较

- 无退化。

> 口径：规则模式不联网、不依赖 API Key，是 CI 快车道跑的；
> 真实模型模式需 `--mode llm` 显式开启，其指标随模型与采样浮动，
> 因此基线回归阈值取 2 个百分点而非 0。

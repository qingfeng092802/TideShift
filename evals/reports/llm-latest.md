# TideShift 对话/解释层评测报告

- 模式：**llm**（模型 `deepseek-flash`）
- 调度日 2024-07-30｜求解器状态 `Final_DR_Adjusted`｜XGBoost 关｜MILP 重跑 打桩
- 代码 `f50a0ec`｜Python 3.13.9｜生成于 2026-09-19 12:14:21
- 用例 35 条，跳过 0 条，执行异常 0 条

## 指标

| 指标 | 值 | 样本 | 含义 |
|---|---|---|---|
| `task_success_rate` | 0.914 | 35 | 四维全对的用例占比（主指标） |
| `tool_call_accuracy` | 0.971 | 35 | 选对工具（含参数匹配）的比例 |
| `answer_completeness` | 0.943 | 35 | 回答要点齐全的比例 |
| `number_fidelity` | 1.000 | 9 | 数字与真实报表一致的比例 |
| `side_effect_guard_rate` | 1.000 | 5 | 不该重跑 MILP 时确实没重跑 |
| `grounding_catch_rate` | 0.750 | 4 | 编造数字被守卫拦下的比例 |
| `grounding_false_positive_rate` | 0.000 | 3 | 真数字被误报的比例（越低越好） |

- 延迟 p50 / p95：5073.0 ms / 28830.7 ms
- token：prompt 132533 / completion 32257（规则模式恒为 0，不参与结论）

## 按用例类型

| 类型 | 用例数 | 任务成功率 |
|---|---|---|
| act | 8 | 0.875 |
| explain | 6 | 1.000 |
| guard | 5 | 1.000 |
| query | 16 | 0.875 |

## 失败用例（3 条）

| 用例 | 类型 | 问题 | 实际调用 | 失败原因 |
|---|---|---|---|---|
| `q-dr-accepted` | query | 需求响应都被接受了吗 | get_dr_info→get_thermal_info | `缺少要点「✅」` |
| `q-dr-rejected-reason` | query | 为什么那个 DR 邀约被拒绝了 | get_dr_info→get_report→get_thermal_info→explain_schedule | `缺少要点「❌」` |
| `l-what-if` | act | 如果只允许充放一次，收益会掉多少？ | compare_baseline→get_report→ask→get_dr_info→get_thermal_info | `未调用期望工具 ['run_with_params', 'explain_day']，实际调用 ['compare_baseline', 'get_report', 'ask', 'get_dr_info', 'get_thermal_info']` |

## 防幻觉守卫探针

| 探针 | 埋入的编造数字 | 被拦 | 漏网 | 误报 |
|---|---|---|---|---|
| `gp-fabricated-money` | ['4521', '0.87'] | ['4521.00 元', '0.87 元/kWh'] | — | — |
| `gp-fabricated-temp` | ['71.5'] | ['71.5 ℃'] | — | — |
| `gp-rounding-ok` | — | — | — | — |
| `gp-count-words` | — | — | — | — |
| `gp-unit-boundary` | ['0.9'] | — | ['0.9'] | — |
| `gp-clean-report` | — | — | — | — |

## 与基线比较

- 基线是 `rule` 模式 32 例、本次是 `llm` 模式 35 例，**分母不同**，上面的差值只能当参考。
- 同分母口径（两侧都覆盖的 32 例）任务成功率：本次 0.938 vs 基线 0.969。
- ⚠️ task_success_rate: 基线 0.969 → 当前 0.914（退化 0.054）
- ⚠️ answer_completeness: 基线 1.000 → 当前 0.943（退化 0.057）

> 口径：规则模式不联网、不依赖 API Key，是 CI 快车道跑的；
> 真实模型模式需 `--mode llm` 显式开启，其指标随模型与采样浮动，
> 因此基线回归阈值取 2 个百分点而非 0。

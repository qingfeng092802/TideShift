# evals —— agent 行为评测

`tests/` 回答"代码按设计跑了吗"。本目录回答另一个问题：**agent 把任务做对了吗**。
前者全绿不代表后者不退化——换模型、改提示词、动工具描述，`tests/` 可能一个都不报红，
而任务成功率已经掉下去了。两个必须分开计量。

## 一分钟跑完

```bash
pip install -r requirements-dev.txt
python -m evals.run_eval                  # 离线规则模式，约 45 秒（内含一次真实 MILP 求解）
open evals/reports/rule-latest.md         # 看报告
```

不需要 API Key、不联网。**这份报告是仓库当前状态的实测结果**，任何人 clone 下来都能复现：

| 指标 | 规则模式（32 例） | 真实模型 `deepseek-flash`（35 例） | 含义 |
|---|---|---|---|
| `task_success_rate` | 0.969 | 0.914（同分母 32 例 0.938） | 四维全对的用例占比（主指标） |
| `tool_call_accuracy` | 0.969 | 0.971 | 选对工具、且参数解析正确 |
| `number_fidelity` | 1.000 | 1.000 | 回答里的数字与真实求解结果一致 |
| `side_effect_guard_rate` | 1.000 | 1.000 | 不该重跑 MILP 时确实没重跑 |
| `grounding_catch_rate` | 0.750 | 0.750 | 编造数字被 `check_grounding()` 拦下的比例 |
| `grounding_false_positive_rate` | 0.000 | 0.000 | 真数字被误报为编造的比例（越低越好） |

## 四个判定维度

一个用例只有四维全对才算通过（`evals/metrics.py::score_case`）：

1. **工具选择** —— 期望的工具是否真被调用，禁止的是否出现，参数是否解析对
   （`expect_tools` / `forbid_tools` / `expect_args`）。
2. **回答要点** —— `must_contain` / `must_not_contain` / `must_regex`。
3. **数字保真** —— `expect_from_report`，锚点取自**运行时真实求解结果**，不写死在 YAML 里。
   这是唯一能抓住"改了模型口径而无人发现"的一维。
4. **副作用守卫** —— `allow_side_effect: false` 的用例若触发 MILP 重跑即判失败。

工具调用由 `evals/recorder.py` 包在 `SchedulingTools` 的方法上记录，
**看的是实际执行了什么，不是模型自述想调什么**——中间还隔着参数校验、异常降级、规则路由。

## 防幻觉守卫自己的评测

`check_grounding()` 的承诺是"LLM 编的数字会被拦下来"。这句承诺的可信度是一个比率，
不是一个例子，所以 `cases.yaml` 的 `grounding_probes` 段主动构造了六类回答：

- 埋入摘要里不存在的金额/温度 → 期望全部被拦；
- 真数字取整、无单位计数词、整段照抄摘要 → 期望**零误报**（拦错等于把守卫变成噪音）。

**当前 0.750 而不是 1.000，是刻意的**：`gp-unit-boundary` 埋的是"放电量约为充电量的 0.9 倍"。
`_UNIT_NUM_RE` 的单位白名单是 `元/kWh|元|kWh|kW|℃|%|次`，不含"倍"，
所以这个编造数字**根本不进入校验**。这是实现的边界而非测试写错——
`tests/test_evals.py::test_known_blind_spot_unit_outside_whitelist` 断言的是"漏网"，
将来把白名单补全后这条断言会红，提醒回来把它翻成"拦住"。
把盲区写进指标而不是从用例里删掉，是这份评测可信度的来源。

## 已知失败用例（真实存在，未修）

| 用例 | 模式 | 现象 | 判断 |
|---|---|---|---|
| `q-baseline-temp` | 规则 | "跟不考虑热的策略比，温度差多少" 被路由到 `get_thermal_info`，未走 `compare_baseline` | 规则路由的长尾。**不建议**再往关键词表里加词——这个分支历史上已经因裸字"调"、裸字"热"各翻过一次车。正解是让这类句子走 LLM 模式；规则模式是无 Key 时的演示兜底。**该用例在 LLM 模式下通过。** |
| `q-dr-accepted`、`q-dr-rejected-reason` | LLM | 判失败原因是 `must_contain` 里的 `✅`/`❌`——规则模板的表面符号 | **判分口径偏保守，不是模型答错**。两问的实际回答比模板更完整（2 事件 1 接受 1 拒绝、达成率 62%、净收益与拒绝原因都对），只是没照抄 emoji。指标照原样记，不为了让数字好看去放宽断言；要修的是把这两条换成语义判定，而不是删掉符号。 |
| `l-what-if` | LLM | 期望 `run_with_params`，模型连调 5 个查询工具却没重跑 | **用例点错了 + 量出了真风险**。`run_with_params` 只有 `soc_min/soc_max/rated_power/include_thermal/include_degradation/enable_dr` 六个旋钮，**没有一个能表达"限制循环次数"**；模型先答"没有这个开关、无法给精确差值"，随后自行估了 400–700 元区间。前者是对的，后者正是"无工具依据时给估算数字"的行为，而这类数字不会被任何守卫拦下（见下）。 |

`g-invent-forecast`（"把明年电价涨 10% 后的收益重算一遍"）目前**通过**，但只是因为它
没有偷偷重跑，也没有声称已重算——它回的是当日报表。答非所问这件事本身没被判失败：
现有关键词维度测不出"回答是否切题"，那需要真实模型模式加语义判定。

## 这条评测已经抓到的真实缺陷

这些不是"测试写坏了"，是评测跑出来的产品 bug（`tests/test_chat_agent.py` 已加回归用例）：

1. **`soc_max` 被截成 0（P0）**：`re.search(r'soc.{0,5}(?:上限|最高|max).{0,5}(\d{1,3})')`
   中间段是贪婪量词，对 README 自己的示例「把SOC上限调到80%重跑」捕获的是 `0`，
   于是**按 SOC 上限 0% 去跑真实 MILP**。改为惰性 `{0,5}?`，并对 SOC 加 1~100 范围校验。
2. **重跑意图被查询分支劫持**：「关掉热约束重新跑一遍」命中裸字"热"→温度查询；
   「不参与需求响应，再算一次」命中"需求响应"→DR 查询。现在显式重跑动词排在查询分支之前。
3. **负向词只看"关"**：「不参与需求响应」会被读成"要参与"。改为 `NEGATIONS` 词表。
4. **报表里有的指标问不到**："等效循环""衰减成本""充了多少度电"全部落到默认兜底语。

## 加用例的规则

- 数字锚点优先用 `expect_from_report`，不要写死数值（写死的数字会在改口径后测出假绿）。
- 需要"期望它拒答/期望它别做事"时，用 `allow_side_effect: false` + `forbid_tools`，
  不要用 `must_contain` 去猜一句人话。
- 只该在 LLM 模式跑的题写 `modes: [llm]`。规则模式跑不到又算成失败，是假阳性；
  报告里的"跳过"段会单列，不混进分母。
- 字段名拼错会被 `validate_suite()` 拒绝（`tests/test_evals.py::test_cases_yaml_is_clean`）：
  未知字段的断言会静默失效，那份评测就会"全绿却什么都没测"。

## 真实模型模式

```bash
export DEEPSEEK_API_KEY=sk-...            # 或 OPENAI_API_KEY / LLM_API_KEY
python -m evals.run_eval --mode llm --model deepseek-flash
```

同一套用例、同一套四维判定，差别只在被路由的是模型而不是关键词表。
跑完对比 `reports/rule-latest.md` 与 `reports/llm-latest.md`，就是
"这个系统里 LLM 到底赚回了多少"的最直接证据。

**已实测一轮**（`deepseek-flash`，2026-09-19，代码 `f50a0ec`，35 例 / 0 跳过 / 0 异常 / 347 秒）：
任务成功率 0.914，**同分母 32 例是 0.938，比规则模式的 0.969 低 3.1 个百分点**；数字保真 1.000、
副作用守卫 1.000、编造捕获 0.750 与规则模式一致。代价是延迟从 0 变成 p50 5 073 ms / p95 28 831 ms，
token 132 533 + 32 257。三条失败里两条是判分口径（`✅`/`❌` 表面符号）、一条是工具面覆盖不到，
**真正的净收益体现在 `q-baseline-temp` 这类规则路由不动的长尾上**，而不是总分。

`--no-stub-act` 会让 act 用例真实重跑 MILP（每条 40 秒级），只在改优化模型时用。

### 真实模型模式暴露出的守卫边界

`check_grounding()` 只挂在**调度解释路径**（`LLMExplainer.explain()`）上，对话 Agent 的回答不回查；
而且它判的是"这个数字有没有在事实摘要里出现过"。实测里这两种情况都会漏过去：

- **派生数字**：模型把 396.47 与 21.6 现算成"净收益 374.9 元"——两个来源数字都是真的，
  差值却不可能出现在摘要里，接上回查只会天天误报；
- **白名单外的量纲**：它引用热模型参数时写 `15000 kJ/K`、`0.001 K/W`，这两个值与
  `src/utils/config.py:40-41` 完全一致（来自工具返回，不是编的），但 `kJ/K`、`K/W` 不在单位白名单里，
  回查根本不会看到它们。

所以正确结论是：**要接对话路径，先给回查加"可验算的派生表达式"这一档**，否则 1.000 的数字保真率
会被换成一片假警报。这也是本项目把它写进 `README.md` 已知局限而不是"顺手修掉"的原因。

## 回归门禁

```bash
python -m evals.run_eval --update-baseline     # 接受当前结果为基线，写 reports/baseline.json
python -m evals.run_eval --check-baseline      # 退化则退出码 1
```

基线文件入库，因此"上周还能做对的任务这周做错了"是可 diff 的事实，不是回忆。
阈值取 2 个百分点：规则模式本身是确定性的，余量留给真实模型模式的采样抖动。
**基线目前只有规则模式那一份**（`meta.mode = rule`），所以 `--mode llm --check-baseline`
是跨口径比较：`compare_with_baseline()` 会额外打印"两侧都覆盖的用例子集"上的同分母成功率，
并且不据此判定失败（`tests/test_evals.py::test_cross_mode_baseline_flags_denominator_mismatch`）。
真要给 LLM 模式上门禁，得先 `--mode llm --update-baseline` 写一份同模式基线——但那是拿
浮动指标当门禁，本项目没这么做。

CI 接法：`pytest -m ""`（nightly 全量）会带上 `tests/test_evals_run.py` 的 `eval + slow`
用例；PR 快车道 `-m "not slow"` 不跑它，改评测层本身的正确性由快车道里的
`tests/test_evals.py`（31 条，不依赖求解器与网络）负责。

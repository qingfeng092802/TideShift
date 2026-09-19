# Changelog

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本（SemVer）。
唯一版本号来源：`src/__init__.py` 的 `__version__`。

## [1.0.0] - 2026-09-19（开源首发）

面向公开仓库重写核心文档与依赖配置（2026-09-14 ~ 09-17）；2026-09-18 起新增 agent
行为评测层、约束参数寻优 Agent 与运行追溯，并修掉评测首轮暴露出的对话路由缺陷（**含运行时改动**）。

### 变更（Changed）—— 版本号与注释口径

- **`__version__` 由 `2.4.4-fix30` 改为 `1.0.0`**。`-fixNN` 后缀是闭源阶段的内部工单号，
  对外只会被读成"这是个没修完的私有分支"；开源首发改用干净的 SemVer，并打 `v1.0.0` 标签。
  本文件下方 2.x 各条目**按当时命名原样保留**，不追溯改写——那是真实历史。
  `web/index.html` 的静态资源 `?v=` 缓存位同步为 `1.0.0`（它只能随发版手改，故纳入发布流程）。
- **源码注释里的内部工单印记全部剥离**（`🔴#4` / `🟠#24/#41` / `P0-04` / `P1-B3` /
  `v1.1：` / `fix30` 等，35 个文件、约 230 处）：只删编号，**编号后面那句真实约束一律保留**。
  例如「`# 与原版一致：…（🔴#4：回写纳入会话锁，消除写竞争）`」变成
  「`# 对话工具可能重跑调度：回写必须在会话锁内，否则并发请求会读到半更新的结果`」——
  印记对新读者是噪声，但那条不变量是读者真正需要的。
- **修掉 20 余处"指着一个不存在的文件"的注释**。`app.py`（早期的 Streamlit 前端）从未进入
  本仓库历史（`git log --all -- app.py` 无记录），却有 15 处注释把它当作规范来源引用，其中
  `PRICE_RULE_VERSION` 那条"与 app.py 保持一致，调整时必须 +1"是**会误导改动者的活指令**。
  同类悬空引用还有"与原版一致"（4 处）、"见审查报告阶段3建议"（2 处）、"独立验证曾实测"（2 处）
  ——那份审查报告也不在仓库里。全部改写为不依赖外部文档的自解释表述。
- **`storage_optimization_agent.py` 的模块级 changelog 块**（6 条 `[P0-x]/[P1-x]` 修复记录）
  改写为"模型口径"说明：演进史归本文件，代码里只留读者用得上的约束。
- `src/agents/llm_explainer.py` 的「为什么这么设计（**面试**可讲的三条）」去掉括号——
  三条设计理由是正当内容，那句备注不是。
- 遗留：`backend/server.py` 仍有 **1717 行**（含 29 个端点与会话管理）。已按注释清理降低可读性
  负担，但**尚未按"认证 / 求解 / 页面数据 / 设置"拆分模块**，这是下一步。

### 新增（Added）—— 2026-09-19 首屏演示动图

- **`docs/screenshots/tideshift-demo.gif`**（1000×625、38 帧、19.8 秒、0.43 MB）：headless Chromium
  通过 CDP 驱动本机真实服务，走完「登录 → 欢迎页 → 强制重算（进度遮罩真跑 MILP）→ 数据总览 →
  电池热管理 96 点温度曲线 → 对话问一句」。没有假界面、没有后期合成；
  **未配置 LLM Key，所以右侧对话是规则模板解释，界面自己也标注了来源**——这一点写进了图注。
- 中英 README 首屏各插入该动图，图注说明录制方式、调度日与"没接 LLM"这一事实。

### 修复（Fixed）—— 录制过程中撞出来的本地环境坑

- **`web/vendor/echarts.min.js` 在本地工作树里带 45 个 CRLF，撞上 `<script>` 上的 SRI
  `integrity` 校验，浏览器直接拒绝执行 → 整个看板报「echarts is not defined」、图表全空白**。
  仓库里的 blob 和 `index.html` 里那串 sha384 **都是对的**（实测：blob 归一化后哈希逐字符相等），
  坏的是这台机器上"在 `.gitattributes` 之前检出"的工作树——git 比对时会做行尾归一化，
  所以 `git status` 显示干净，看不出任何异常；SRI 却按字节算，40 字节的差异就是致命差异。
  新克隆不受影响。**修复方式**：`git show HEAD:web/vendor/echarts.min.js > web/vendor/echarts.min.js`
  （`web/css/app.css` 1159 处、`web/js/markdown.js` 129 处同理，它们没有 SRI 所以只是不美观）。
  已作为常见问题写进两份 README 的 FAQ——这个坑值得被单独说一次：**行尾差异在"有 SRI 的静态资源"上
  会从 cosmetic 升级成 hard failure**。

### 新增（Added）—— 2026-09-19 真实模型评测（`--mode llm`）

- **补上此前欠下的那一轮真实模型回归**：`python -m evals.run_eval --mode llm --model deepseek-flash`
  跑通 35 例（0 跳过 / 0 异常 / 347 秒 / 16.5 万 token），报告入库为
  `evals/reports/llm-latest.{json,md}`。**写的是实测值而不是好看的值**：任务成功率 0.914，
  换算到与规则模式**同分母的 32 例是 0.938，仍低于规则模式的 0.969**；数字保真 1.000（9 例带锚点）、
  副作用守卫 1.000（5 例）、编造捕获 0.750、误报 0.000 与规则模式一致；延迟 p50 5 073 ms /
  p95 28 831 ms（规则模式恒为 0）。
- **三条失败逐条归因，没有放宽任何断言**：`q-dr-accepted`、`q-dr-rejected-reason` 是被
  `must_contain` 里的规则模板表面符号（`✅`/`❌`）判掉的，模型回答的事实部分完整且正确；
  `l-what-if` 是**用例点错**——`run_with_params` 的六个旋钮没有一个能表达"限制循环次数"，
  模型先答"没有这个开关"（这句是对的），随后自行估出 400–700 元区间（这才是被量出来的真风险）。
  口径问题写进 `evals/README.md` 的已知失败用例表，指标原样保留。
- **暴露出守卫的作用域边界**（记入两份 README 的已知局限）：`check_grounding()` 只挂在调度解释路径上，
  对话回答不回查；而"摘要里两个数字相减"这类派生数字、`kJ/K` 这类白名单外量纲本来就不在它的能力范围内。
  没有顺手把回查接到对话路径，因为那会把 1.000 的数字保真率换成一片误报。

### 变更（Changed）—— 跨模式比较的分母口径

- **修掉一个会产出假结论的比较**：`--mode llm` 与规则模式基线比较时，报告原本直接给出"退化 0.054"，
  而其中一部分原因只是**分母不同**（llm 35 例 vs rule 32 例）。新增
  `evals/run_eval.py::compare_with_baseline()`：模式或用例数不一致时，补一行"两侧都覆盖的用例子集"
  上的同分母成功率（本例 0.938 vs 0.969），并且**不把跨口径差值当门禁结论**（`--check-baseline` 只告警）。
  规则模式报告逐字节未变（用存量 JSON 重渲染验证）。用例
  `tests/test_evals.py::test_cross_mode_baseline_flags_denominator_mismatch`。
- 测试规模随之 **232 → 233**（快测 200 → 201，`test_evals.py` 30 → 31 条），两份 README 与
  `evals/README.md` 的计数同步。

### 变更（Changed）—— 版权署名与提交元数据

- `LICENSE` 与两份 README 的版权署名由真实姓名改为 GitHub handle `qingfeng092802`；随后用
  `git filter-branch` 把**全部历史提交的 author/committer 元数据**统一为该 handle 的 noreply 邮箱
  （树内容不变，只改元数据）。**副作用如实记录**：所有提交 SHA 改变，因此
  `evals/reports/llm-latest.md` 里记的 `代码 f50a0ec` 在当前历史中已不可达（同一棵树现在是 `3316674`）；
  重写过程丢掉了本地 `refs/remotes/origin/main`，而**远端 `main` 与 `v1.0.0` 标签仍是重写前的历史，
  尚未同步**——要让远端跟上必须 `push --force` 并重打标签，这一步未做。

### 新增（Added）—— 2026-09-18 agent 行为评测层

- **`evals/` 评测层**：与 `tests/` 分开计量。`tests/` 答"代码按设计跑了吗"，
  `evals/` 答"agent 把任务做对了吗"。用例定义在 `evals/cases.yaml`
  （35 条对话用例 + 6 类防幻觉探针），**数字锚点取自运行时真实求解报表**而非写死在文件里，
  因此换模型、改提示词、动电价口径都会被抓到。
  每个用例四个维度全对才计通过：工具选择与参数解析、回答要点、数字保真、副作用守卫。
  工具调用由 `evals/recorder.py` 包在 `SchedulingTools` 方法上记录，看**实际执行了什么**，
  不信模型自述想调什么。
- **`evals/grounding.py`**：把"防幻觉回查有效"从一句举例变成比率指标——向回答里主动埋入
  摘要中不存在的数字量捕获率，用真数字取整/无单位计数词量误报率。
  取代了 `docs/experiments.md` 一直引用、但**从未入库**的 `_eval/verify_v12.py`
  （`git log --all --diff-filter=A` 查无此文件）。
- **`evals/run_eval.py` CLI**：`--mode rule`（离线确定性，默认，45 秒）/ `--mode llm`
  （真实模型，凭据走环境变量）/ `--update-baseline` / `--check-baseline`（退化则退出码 1）/
  `--no-stub-act`。报告落 `evals/reports/{rule,llm}-latest.{md,json}`，
  基线 `evals/reports/baseline.json` 入库，使"上周做对的任务这周做错"成为可 diff 的事实。
- **`tests/test_evals.py`（30 项，快车道）**：测尺子本身——分母口径（没声明数字锚点的用例
  不得进"数字一致率"分母，否则该指标恒为 1）、打桩是否真的挡住了 MILP、用例文件字段拼写
  （未知字段会让断言静默失效、评测"全绿却什么都没测"）。
- **`tests/test_evals_run.py`（8 项，`eval + slow`）**：nightly 跑，含与基线的回归比对。
- **`pytest.ini` 注册 `eval` 标记**；**新增 `.gitattributes`** 锁定行尾——
  仓库内 blob 一直是 LF，但那是各人本地 `core.autocrlf=true` 的巧合而非约定。

首轮实测（调度日 2024-07-30，Python 3.13.9）：任务成功率 **0.969**（31/32），
数字保真 **1.000**，副作用守卫 **1.000**，编造数字捕获 **0.750**（3/4），守卫误报 **0.000**。
未达 1.0 的那一次漏网是已登记的实现边界（单位白名单不含"倍"），
在 `tests/test_evals.py` 里以"断言漏网"的形式钉住，而非从用例里删掉。

### 新增（Added）—— 2026-09-18 约束参数寻优 Agent

- **`src/agents/parameter_search_agent.py`**：把"模型决定控制流"补进主链路。此前编排是
  固定有向图（预测 → MILP → DR → 解释），LLM 只在末端写文案 —— 那是工作流不是智能体。
  新模块跑 提案约束 → 真实 MILP 评价 → 独立复核 → 反思 → 再提案 的闭环，
  模型可主动输出 stop 结束循环。
- **`HeuristicProposer`（确定性坐标下降）**：无 Key 也能跑完整循环，更重要的是充当
  **非智能基线** —— 没有对照物，"agent 有效"就没有内容。`LLMProposer` 在无 Key、
  调用失败、JSON 不可解析、参数越界、提议安全策略项五种情况下逐步回退到它并计数。
- **五条护栏**，每条都有测试：
  ① 越过 55 ℃ 停机线的解可评价不可选中（`allow_unsafe_winner` 默认 False）；
  ② 搜索跑 24/48 点、胜出者必须回到 96 点与默认同场复验（同一配置跨分辨率实测
  1285.79 / 1279.09 / 1226.94 元，不可比）；复验入口对非 96 点直接抛错 ——
  协调器按当日 CSV 取 96 点价格，改 `num_steps` 会让 MILP 只取前半天的数据，
  静默解一个错问题还解得又快又"最优"；
  ③ `include_thermal` / `enable_dr` 属安全与结算策略项，寻优提议即拒收
  （粗分辨率把峰值温度低估约 12 ℃，搜索阶段看不见越温限后果）；
  ④ 胜出门槛 2%：`mip_gap=1%` 下同配置重复求解实测抖动约 1%
  （三次 1244.24 / 1255.87 / 1260.58 元），小于门槛的差异是求解器噪声；
  ⑤ 允许 agent 输：复验未跑赢就如实写"未跑赢"并采用默认配置。
- **`tests/test_parameter_search.py`（49 项）**：假目标函数是"已知参数的已知函数"，
  于是"有没有找到更优"有唯一答案；可行性判据复用生产代码的 `_rejections`，
  不在测试里另写一套规则。含 1 项 `slow` 真实 MILP 用例。
- **修复判据缺陷 1 处**：`_status_infeasible` 不能用 `"feasible" in status` 判可行，
  因为 PuLP 的 `Infeasible` 本身就含子串 `feasible` —— 会把不可行解放过去。
  现按"已知不可行"名单判，并覆盖项目里全部真实状态串
  （`Optimal` / `Infeasible` / `Not Solved` / `Stopped (time limit, feasible incumbent)` /
  `Final_DR_Adjusted` / `Baseline`）。
- **`docs/parameter-search-sample.md`**：一次真实运行的报告。结论是**启发式寻优未跑赢
  默认约束**（降功率、收窄 SOC 窗口都让净收益下降），如实入库而非挑一次成功运行展示。
- CLI 报告含 ✅/℃ 等字符，Windows 控制台默认 GBK 会 `UnicodeEncodeError` 直接跑挂；
  `main()` 里把 stdout/stderr 重设为 UTF-8（开发与实测环境即 Windows 10）。

### 新增（Added）—— 2026-09-18 运行追溯（trace）

- **`src/utils/trace.py`**：每次运行（求解 / 对话 / 解释 / 寻优循环）落一行 JSON 到
  `logs/traces/trace-YYYYMMDD.jsonl`，并进进程内有界环形缓冲。日志答"哪里报错"，
  trace 答"这一次运行里每步耗时多少、LLM 走了哪条降级路径"。不引 Langfuse / OTel，
  与项目"完全离线可用"的口径一致。
- **接入点**：`/api/solve` 后台求解线程、`/api/chat`、`/api/chat/stream`、`/api/explain`、
  参数寻优 Agent 的每一发求解、解释层的 LLM 调用（含 `status=degraded` 与错误类型）。
  无活动 run 时 `span()` 完全空转，import 本模块不改变任何既有行为。
- **只读接口**：`GET /api/traces?limit=20`（摘要，含最慢环节）、
  `GET /api/traces/{run_id}`（完整事件序列）。均在 JWT 之后；`run_id` 只作内存查询键，
  **不参与文件路径拼接**（否则就是现成的目录穿越入口，已有用例守着）。
- **按级脱敏**：`ENERGY_TRACE_LEVEL` 三档。默认 `basic` 只记事件名、耗时、状态与长度计数
  —— 问题文本会变成 `question_len`；`full` 才连文本一起记（截断 2000 字）。
  对话里可能出现用户自己上传的负荷数据，默认不落盘。
- **两个必炸的地方专门测了**（`tests/test_trace.py`，18 项）：
  ① 活动 run 必须**线程局部**——求解跑在后台线程，用模块级全局会把并发请求的事件
  混进同一个 run，trace 就成了假证据；② `@trace.traced` 必须用 `functools.wraps`
  保住签名，否则 FastAPI 注入不了请求模型。另测盘不可用时不拖挂主流程、环形有界、
  `off` 级彻底静默。
- 新增环境变量：`ENERGY_TRACE_DIR` / `ENERGY_TRACE_LEVEL` / `ENERGY_TRACE_MAX`，
  已写入 `.env.example` 与 README 配置项表。
- 已知未完成项：**看板的追溯页未做**。`/api/traces` 已可 curl，前端页面留给后续。

### 修复（Fixed）—— 由 evals/ 首轮评测暴露，均有回归用例

- **P0：SOC 参数被截断，系统按错误约束跑真实 MILP**。
  `RuleBasedAgent` 的参数正则 `soc.{0,5}(?:上限|最高|max).{0,5}(\d{1,3})` 中间段是贪婪量词，
  对「把SOC上限调到80%重跑」（**README 自己的示例句**）捕获的是 `0`，
  于是以 `soc_max=0%` 入求解器。改为惰性 `{0,5}?`，并对 SOC 加 1~100 范围校验，
  越界值不传且留日志。回归：`test_param_extraction_from_utterance`。
- **重跑意图被查询分支劫持**：「关掉热约束重新跑一遍」命中裸字"热"→温度查询，
  「不参与需求响应，再算一次」命中"需求响应"→DR 查询，用户要的真实重算从未发生。
  现在显式重跑动词排在所有查询分支之前；抽不到参数时不拦断查询
  （「帮我重新算一下和基准的对比」仍走对比分支）。
- **负向词只判"关"**：「不参与需求响应」会被读成"要参与"。改为 `NEGATIONS` 词表
  （关 / 去掉 / 不用 / 不参与 / 取消 / 禁用 / 不要）。
- **重跑动词缺"再算"**：只列了"重算/重新算"，中文里更常见的「再算一次」不命中。
- **报表里已有的指标问不到**："等效循环多少次""充了多少度电""衰减成本多少"
  全部落到默认兜底语。`get_report` 的关键词改为覆盖"报表里有什么"而非只覆盖"钱"。
- **`SchedulingTools._digest()` 改为公开 `current_digest()`**：事实摘要是
  "LLM 允许引用的全部数字"，核对出处的调用方不该各自反推。


### 变更（Changed）

- **`README.md` 重写**：改为面向外部开发者的结构——项目简介 / 核心功能 / 系统架构（Mermaid）/
  环境依赖 / 安装 / 快速开始与首启登录 / 使用说明（页面、数据上传、LLM 配置、API 一览）/
  配置项 / 项目结构 / 核心技术细节 / 量化成果 / 测试 / 常见问题 / 贡献指南 / 路线图 / 许可证。
  移除面向个人求职的表述；**保留全部实测数据表及其口径脚注**（含"未复核"标注）。

- **`requirements.txt` 约束收口**：由 `~=` 兼容发布区间改为 `>=下界,<上界` 显式区间
  （下界 = 实测版本，上界 = 下一个可能破坏兼容的版本）。原因：`langchain~=1.3` 等价于
  `>=1.3,<2.0`，实测全新安装会解析到 langchain 1.4.0 / langchain-core 1.6.3 /
  langchain-openai 1.6.2 等**未经验证的版本**。收口后重新实测：直接依赖不再跨次版本跳变
  （langchain 1.3.x、langgraph 1.2.x、langchain-core 1.5.x），唯一例外是 uvicorn
  （0.52.1 → 0.53.0，Web 栈按主版本收口）。
  另补声明 `langchain-core`——它被 `src/agents/chat_agent.py` 直接 import。

- **`requirements.lock` 升级为完整锁**：由「21 条直接依赖」扩为**包含全部传递依赖的完整锁**
  （76 个包 = 直接 20 + 传递 56），并修正原注释的误导——它并非 pip freeze 全量冻结。
  锁文件不含 hash，原因（跨平台 / hash 按平台记录）已写入文件头。

- **`requirements-dev.txt`**：改为与 `requirements.txt` 同口径的显式区间。

- **`.gitignore` 扩充**：补齐构建产物、静态检查缓存、编辑器与系统文件、备份文件；
  密钥与运行时状态（`config/`、`.solve_cache/`、`.env`、`logs/`）的排除口径保持一致。

- **`.env.example`**：补用法说明头部。

- **`LICENSE`**：MIT，版权署名 qingfeng092802。

- **新增 `THIRD_PARTY_NOTICES.md` + `licenses/`**：声明随仓库分发的 Apache ECharts 5.6.0
  （Apache-2.0），以及其内嵌的 ZRender（**BSD 3-Clause**，非 Apache-2.0）与 Microsoft 代码片段
  （0BSD）。归档 ECharts 上游 NOTICE（满足 Apache-2.0 第 4(d) 条）与两份许可原文。

- **新增 `docs/experiments.md`**：把 README 中被压缩的完整实验数据、ablation 明细、
  LLM 解释层评测与**未复核项清单**移入独立文档，README 只留结论与链接。

- **新增 `docs/screenshots/`**：真实界面截图（欢迎页 / 数据总览浅色 / 充放电调度 /
  电池热管理 / 数据总览深色）。由 Playwright 驱动真实登录与求解流程生成，未改动任何前端代码。

- **新增 `docs/experiments.md` 的 Web 端实测小节**：记录 2026-09-16 在调度日 2024-07-15 上的
  端到端实测（服务端日志 `求解完成 key=cc6c36b0 用时=26.5s`），含当日收益/温度/循环数与
  链路健康判据（负荷预测 MAPE 2.88% vs 朴素基线 2.85%、物理修正近零、解释层降级 rule）。

- **README 求解耗时口径校正**：原写「首次约 40 秒」，实测 2024-07-15 为 **26.5 秒**，
  2024-07-30 为 38~43 秒。已改为「本机实测 **26 ~ 43 秒**，随当日规模与机器性能变化」。

- **README 补「调度日口径」警示**：量化成果表为 2024-07-30，而系统默认调度日是内置演示数据
  的最后一天（2024-07-15），首次打开看板看到的数字与表不同，已显式说明两者不可横向比较。

### 修复（Fixed）· 首轮对外评审发现的问题

- **🟠 README 安装步骤与依赖不一致**：安装章节在 `pip install -r requirements.txt` 之后直接给
  `pytest -q`，但 `pytest` 属 `requirements-dev.txt`——只装运行依赖的用户执行该命令必然失败。
  已拆为「运行依赖 → 可选开发依赖 → 可选验证」三步，并显式标注 pytest 的归属。

- **🟠 `requirements.lock` 描述自相矛盾**：依赖表写「不含传递依赖」，安装章节又说「逐版本完全
  一致」。已按「完整锁」重做（见上），两处口径统一。

- **🟠 README 残留发布占位符**：`git clone https://github.com/<owner>/<repo>.git` 与顶部
  `<!-- TODO -->` 的 CI 徽章注释块。已改为**零占位符**写法（clone 章节用「进入项目根目录」表述，
  徽章块移除）。

- **🟠 静态 Tests 徽章与正文不一致**：徽章写 `70 passed`，正文写「约 90+ 项」。已统一为实测值
  **92 项**（70 快测 + 22 slow），并在测试章节写明拆分。

- **🟡 量化成果表混入未复核数据**：脚注中的「沿用早期记录/未复核」行移入 `docs/experiments.md`
  的**未复核项清单**，README 主表只保留已复核项。

- **🟡 文档未覆盖 vendored 第三方许可**：已补 `THIRD_PARTY_NOTICES.md` 与 `licenses/`。

- **🟡 `pytest -m ""` 在 PowerShell 下失效**：README 测试章节原先给出该命令，实测在 PowerShell
  下报 `argument -m: expected one argument`（空字符串参数被 shell 丢弃）。已改为
  `pytest -o addopts= -q` 并给出等价表达式与平台说明。

- **🟡 公网部署提示与数据来源声明不够显眼**：已提升为顶部两段独立提示块。

### 修复（Fixed）· 第三方测试报告（F2）

外部测试报告（2026-09-16，Windows / 隔离 venv / `requirements.lock`）结论为「可发版」，
P0/P1 无项；其中 P3 项 F2 已在本轮修复：

- **🟠 解释层与对话 Agent 未返回机器可读的来源标识**
  `/api/explain` 只返回 `{"text": ...}`，`/api/chat` 只返回 `{"reply", "history"}`——
  来源信息仅编码在正文前缀（`🤖 LLM 决策解释` / `📋 规则模板解释`）里，调用方只能靠字符串
  匹配判断走没走 LLM。前端因此存在**可见缺陷**：点「生成 / 刷新解释」后只替换正文，
  标题里的来源标签停留在旧值（例如解释已生成完，标签仍显示「⚪ 未生成」）。
  修复：
  1. `SchedulingTools` 新增 `last_explain_source`（`"llm" | "rule" | "none"`），
     `explain_day()` 写入来源；
  2. `RuleBasedAgent.mode = "rule"`、`LLMAgent.mode = "llm"`，由 `create_agent` 工厂产出；
  3. `/api/explain` 返回 `source`；`/api/chat` 与 `/api/chat/stream` 的收尾事件返回 `mode`；
  4. 前端 `aiExpander` 抽出 `aiExpanderHeadInner()` 单一来源标签实现，刷新解释时同步
     更新标题标签与展开态（用 `innerHTML` 替换按钮内容而非 `outerHTML`，避免丢事件监听）；
  5. 新增 5 项测试：规则/LLM 模式标识、`last_explain_source` 的 `rule` 与 `none` 两条路径、
     两个端点的响应契约（含 `source` / `mode` 字段存在性）。

- **🟡 测试文件内的失效登录 Helper（顺带修复）**
  `tests/test_server_api.py` 的 `_login()` 默认 `username="tester"`，但 `AuthStore` 只维护
  单账户（`admin`，且用 `hmac.compare_digest` 全等比较），该默认值必定返回 401。
  已把默认值改为 `server.AUTH.username`，并新增 `_auth_headers()` 便捷函数；
  新增用例实际调用过该路径后才发现此问题。

### 修复（Fixed）· 第三方核验报告闭环（Bug 2~6）

外部测试方对一份带 AIGC 标识的报告逐条核验后给出「6 条中 5 条属实」的判定。逐条自证与处置：

- **🔴 Bug 2（阻断级）· 跑完 `pytest` 后服务忽略 `ADMIN_INITIAL_PASSWORD`，任何口令都登录失败**
  **本轮最高优先级修复。** 报告称该问题"已在测试副本修复"——但那是对方副本，本仓库**未修**，
  且它是可复现的：`backend/server.py` 在**导入时**即实例化 `AuthStore`，而 `_config_dir()`
  默认指向 `<项目根>/config/`，因此跑一次 pytest 就会在工作树留下 `config/auth.json`、
  `config/.auth_secret`、`config/.api_secret`。用户按 README「装依赖 → 跑 pytest → 启动服务」
  操作时，服务读到测试生成的随机口令而跳过 `_create_default()`，环境变量被**静默忽略**。
  修复分四处：
  1. `auth.py`：`_config_dir()` 支持 `ENERGY_CONFIG_DIR` 覆盖（三个凭据文件同源受益）；
  2. `tests/conftest.py`：模块级把配置目录与缓存密钥目录重定向到会话级临时目录，
     并在会话结束时清理——测试状态与仓库工作树彻底隔离；
  3. `auth.py`：已存在口令文件且检测到 `ADMIN_INITIAL_PASSWORD` 时**显式告警**
     （原实现是静默忽略，用户无从自查），并给出恢复动作；
  4. `README` / `.env.example`：补 `ENERGY_CONFIG_DIR` 说明与该故障的恢复步骤（常见问题）。
  验证：清空 `config/` 后跑 pytest，`git status --porcelain` 只列出源码改动，**无任何 config/ 污染**。

- **🟠 Bug 3 · 负荷预测"≥8 天"门槛是错的，真实门槛 11 天**
  机制确认：`dropna()`（`lag_672` 占 7 天）→ 尾部固定切走 `test_days*96`（3 天）→ 训练样本须
  ≥ `MIN_TRAIN_ROWS`（96 点 = 1 天）。即 `N*96 - 672 - 288 >= 96` → **N ≥ 11**。
  修复：新增纯函数 `required_history_days(test_days=3)` 作为**唯一口径来源**，
  告警日志与面向用户的 `fallback_reason` 改由它生成；同步修正 `README`（两处）与源码注释。
  实测边界：10 天 `is_trained=False`、11 天 `is_trained=True`。

- **🟠 Bug 4 · 温度断言过拟合单次求解值（余量仅 0.51℃）**
  实测确认：2024-07-30 后验峰值 **46.988℃**，旧断言上界 `temp_normal_max + 2.5 = 47.5℃` ——
  余量 0.51℃，而 Web 路径的 2024-07-15 为 48.3℃，换求解器版本或换调度日即失败。
  修复：把"观测值当上界"改为**显式预算分解**（`DESIGN_OVERSHOOT_BUDGET_C` 设计裕度 +
  `SOLVER_TOLERANCE_C` 求解器数值容差，各自注明来源），硬红线仍兜底；
  并**新增对照组断言** `test_thermal_constraint_actually_lowers_peak`——关闭热约束后峰值必须
  显著更高（实测 46.99℃ vs 57.12℃，机制降温 **10.14℃**）且必须越过安全停止线。
  这样测的是"机制是否生效"，而不是"是否等于某次解"。

- **🟡 Bug 5 · 对话误路由：裸字 `"调"` 使「调度/协调」类提问进入调参分支**
  修复：取消单字匹配，改为**动作词 + 参数对象同时命中**才进调参分支（明确的"重跑"类动词除外）；
  并把「策略」补进整日解释分支，使「今天的调度策略是什么？」正确返回调度解释。
  （只把"调"换成"调一下"并不够——「协调一下」同样含该子串，已用测试覆盖。）

- **🟡 Bug 6 · 默认调度日与 `dr_signals.csv` 的文档口径错误**
  实测确认：`default_date()` 对 30 天数据返回 **2024-07-15（中间日）**，
  README 却写成"最后一天"；`dr_signals.csv` 的加载函数 `load_dr_signals()` **全局从未被调用**，
  Web 流程的 DR 事件是按所选调度日**硬编码生成**的两组，改该 CSV 不会影响界面。
  修复：改正 README 的默认日描述与项目结构注释，并新增两条常见问题
  （"DR 事件是哪来的""默认调度日是哪一天"）。**未改 `default_date()` 行为**——
  它刻意不硬编码日期，避免内置数据换区间后默认日落在数据之外；行为与文档现已一致。

- **🔴 新发现（本轮自查）· ML 测试是假阴性：从未训练过 XGBoost**
  修复 Bug 3 时顺带发现：`tests/test_load_forecast_ml.py` 用 10 天合成数据（低于 11 天门槛）
  → `is_trained=False`、`model=None`，测试实际走的是朴素基线降级路径，而原断言
  `mape < 15.0` 恒真（`mape` 恒为 0）。**根因有两层**：① 天数不足；② 夹具
  `pd.date_range(end="2024-07-30", ...)` 的终点是**当日 00:00**，故"预测日"只有 1 行，
  `len(forecast_day_data) >= 96` 不成立 → `actual_load=None` → MAPE 从不计算。
  修复：夹具默认改为 14 天且 `end_date` 落到当日 23:45；用例内**先断言真的走了 ML**
  （`is_trained is True`、`fallback_reason == ""`、`physical_correction_applied is True`）
  再断言精度，杜绝降级路径蒙混通过。

### 变更（Changed）· 登录与口令管理（面向开源使用者）

- **新增认证模式 `ENERGY_AUTH_MODE`**，把「口令从哪来」这件事显式化：

  | 模式 | 口令来源 | 适用 |
  |------|---------|------|
  | `persistent`（默认） | 首启读 `ADMIN_INITIAL_PASSWORD`，无则生成随机口令；此后以 `config/auth.json` 为准 | 本机长期使用、单机部署 |
  | `env` | **每次启动**都取 `ADMIN_INITIAL_PASSWORD`，不落盘 | 容器 / CI / 演示（改环境变量即生效） |

  `env` 模式从结构上消除了"改了环境变量却不生效"这一整类困惑；非法取值回退
  `persistent` 并告警（不因配置写错拒绝启动）。应用内改密在该模式返回 **400 + 可执行提示**，
  而不是"看起来成功其实无效"。

- **首启随机口令不再"只打印一次"**：同时以 0600 写入 `<配置目录>/INITIAL_PASSWORD.txt`
  一次性副本，首登改密成功后自动删除。原实现下容器日志被滚掉或用户漏看控制台就再也拿不到
  口令，唯一出路是手工找到并删除 `auth.json`——对开源使用者门槛过高。
  另外首启生成的 `auth.json` 改走 `_atomic_write`，与后续保存的权限处理保持一致。

- **新增恢复入口 `backend/manage.py`**（纯标准库，无第三方依赖）：
  - `python -m backend.manage show-state`：只读报告——认证模式、配置目录及其覆盖来源、
    口令文件状态、环境变量是否正被忽略、一次性口令文件是否存在。**只读、无副作用**（已实测
    执行后配置目录仍为空）。
  - `python -m backend.manage reset-password [--password ...]`：重置并打印新口令；
    `env` 模式下退出码 2 并说明正确改法。
  - 配套新增 `AuthStore(auto_create=False)`：运维命令不会在过程中顺手生成一份随机口令
    （否则控制台会先后出现两个口令，使用者无从分辨）。

- **文档**：README 的「首次登录」重写为「登录与管理员口令」——模式对比表、恢复命令、
  安全设计说明（含"JWT 绑定 UA 指纹，换客户端会 401 并提示登录环境已变化"这一容易误判为故障的行为）；
  `.env.example`、配置项表、项目结构、常见问题同步更新（新增两条 FAQ）。

- **新增 5 项测试**（`tests/test_auth_security.py`）：模式解析、env 模式不落盘、
  首启一次性口令文件的写入与删除、env 指定口令时不生成一次性文件、`auto_create=False` 不生成口令。

> **未做（刻意）**：不提供"关闭认证"开关、不做多用户、不接 OIDC/GitHub 登录。
> 理由：认证常开是本项目的展示点之一；关闭认证的演示模式一旦被误开到公网即是严重风险；
> 多用户对"储能调度算法"这个核心价值贡献有限却会引入用户表、权限与重置流程；
> OIDC 需要外部回调配置，会破坏"完全离线可跑"。这些属产品定位选择，非技术限制。

### 修复（Fixed）· 评审闸门闭环（Windows 权限 / env 语义 / 启动横幅 / 硬测试 / CI）

评审结论为"架构不用返工"，但列出 5 条发版前的硬闸门。逐条落地：

1. **Windows 权限不再是"0600 说说而已"**
   此前 `_atomic_write` 只做 `os.chmod(0o600)`——而 **`os.chmod` 在 Windows 上只能切换只读位**，
   实测写完 `0o600` 后 `stat` 仍是 `0o666`，即"0600 的一次性口令文件"在 Windows 上不成立。
   新增 `_harden_permissions()`：POSIX 走 `chmod 0600`；Windows 额外执行
   `icacls <file> /inheritance:r /grant:r "<当前用户>:F"` 切断继承并只授当前用户
   （授权用 `F` 而非 `R`，否则后续 `_atomic_write` 的 `os.replace` 会因无权删除目标而失败）。
   `icacls` 不可用时记录告警并退回"依赖目录 ACL"，不再把 0600 当跨平台安全边界。
   实测 ACL 仅剩「当前用户 / `NT AUTHORITY\SYSTEM` / `BUILTIN\Administrators`」，普通用户组已移除。

2. **`env` 模式未设 `ADMIN_INITIAL_PASSWORD` 的语义定死为「拒绝启动」**
   新增 `AuthConfigError`：该情形下**不**静默回退 `persistent`（会让用户以为 env 生效、实际写盘），
   也**不**生成随机口令（口令会随重启变化且只打印一次，无法运维），而是
   **服务拒绝启动 + 退出码 2 + 打印可执行的改法**（设 `ADMIN_INITIAL_PASSWORD`，或去掉 `ENERGY_AUTH_MODE`）。
   配置错误在启动时暴露，而不是等到用户打开登录页。

3. **启动横幅打印「认证模式 + 口令来源」**
   仅对非法取值告警不够——容器日志一刷就过去。现在启动即打印一行结论，
   例如 `认证模式: persistent ｜ 口令来源: 口令文件 …/config/auth.json（环境变量只在首次创建且无此文件时生效；当前它**不会生效**）`
   ——把"我改了环境变量为什么不生效"的答案直接摆在日志里。四种状态（persistent 首次创建有无环境变量 /
   已有文件 / env）文案均已实测。

4. **补两条硬测试（评审指定）**
   - `test_env_mode_login_creates_no_auth_file`：**HTTP 层**走完登录与受保护端点后，配置目录下
     不得新增 `auth.json` 与一次性口令文件（只测 AuthStore 不够，登录端点/中间件都可能间接落盘）；
   - `test_stale_initial_password_file_cleaned_when_must_change_false`：`must_change=false` 时启动
     自动清理残留的一次性口令文件（崩溃中断、手工改密、替换 `auth.json` 都可能把它留下，
     那等于磁盘上长期躺着一份可读的初始口令）；并配一条反向约束
     `test_initial_password_file_kept_while_must_change_true`，避免误删导致用户还没登录就丢了唯一口令来源。
   实现上把 `_load()` 的 `try` 收窄到只包"读文件+解析"：后续清理与日志若被 broad except 吞掉，
   会静默走到 `_create_default()` 重新造一份凭据——那是最危险的失败模式。

5. **CI 补上标准 pytest 的按需全量入口**
   全量原先只在 nightly 定时跑，本机又因受限沙箱跑不了 pytest，导致"标准入口全绿"没有可复现出口。
   `.github/workflows/ci.yml` 增加 `workflow_dispatch`，`full-tests` 作业改为
   `schedule || workflow_dispatch` 触发（PR/push 仍跑快测），可在 Actions 页面一键跑全量（含 `slow`）。
   README 明确写：**发版门槛 = CI 全量 `pytest`**，替代验证不能代替标准入口。

> **升级兼容性（重要，需写入 PR 描述）**
> 已有 `config/auth.json` 的用户升级后**默认仍是 `persistent`**：设置 `ADMIN_INITIAL_PASSWORD`
> **不会生效**（该变量只在首次创建口令文件时读取）。这是刻意行为——否则你在界面上改过的口令
> 会在下次重启时被环境变量覆盖回去。想改用 `env` 语义必须**显式**设置 `ENERGY_AUTH_MODE=env`。
> 启动横幅已把这一点直接打出来，无需翻阅文档即可自查。

### 验证（Verified）

- `pytest` 快测 **79 项通过**、全量（含 slow）**102 项通过**（Python 3.13.14 + `requirements.lock`；
  本轮修复 Bug 2~6 并新增 5 项测试，测试总数由 75/97 增至 79/102，README 徽章与测试章节已同步）。
- **Bug 2 隔离效果实测**：清空 `config/` 后跑完整测试套件，`git status --porcelain` 只列出源码改动，
  **无任何 `config/` 文件被创建**——测试不再污染仓库工作树。
- **Bug 3 边界实测**：10 天数据 `is_trained=False`、11 天 `is_trained=True`（与 `required_history_days()` 一致）。
- **Bug 4 对照组实测**：峰值温度含热约束 **46.99℃** / 无热约束 **57.12℃** → 机制降温 **10.14℃**，
  且无约束工况越过 55℃ 安全停止线（对照组具备判别力）。
- **Bug 5 路由核验**：「今天的调度策略是什么？」「帮我协调一下充放电安排」「调度结果解释一下」
  均不再进入调参分支；「帮我调整一下参数」仍正确进入。
- **`ENERGY_CONFIG_DIR` 覆盖优先级核验**：默认→项目 `config/`；仅 `AUTH_CONFIG_DIR`（兼容别名）→ 生效；
  仅 `ENERGY_CONFIG_DIR` → 生效；两者同时设置→以 `ENERGY_CONFIG_DIR` 为准；仅空白值→忽略。
- F2 修复经**真实 uvicorn 端到端**复验：`POST /api/explain` → 字段 `['source','text']`、
  `source='rule'`；`POST /api/chat` → 字段 `['history','mode','reply']`、`mode='rule'`。
- `node --check web/js/pages.js` 语法校验通过（前端改动无语法错误）。
- `git add -A` 后纳入版本控制 **77 个文件**；`config/`、`.env`、`logs/`、`.solve_cache/`
  经 `git check-ignore` 确认均被正确排除。
- 后端启动冒烟：`/api/system-info` 返回版本 `2.4.4-fix30`；未带 token 访问
  `/api/page/dashboard` 返回 401（认证门生效）；首启经 `ADMIN_INITIAL_PASSWORD` 登录返回 200；
  未求解时该端点返回 409（预期行为）。
- `pip install --dry-run --report` 实测：`requirements.txt` 与 `requirements.lock` 均可解析，
  无依赖冲突；收口后直接依赖不跨次版本跳变。
- 截图由 Playwright 驱动真实 UI 生成（登录 → 自动求解 → 逐页截图），未改动任何前端代码。
- 缓存命中时 `/api/progress` 返回 `{"running":false,...,"solved":true}`（布尔字段，
  JSON 紧凑格式无空格）——作为「求解已完成」的判据时应按字段解析，不要做字符串匹配。

### 说明（Notes）

- 源码注释、测试与前端脚本中的内部审查编号（`🔴` / `🟠#24` / `P0-02` 等）**本次未清理**：
  它们对应一份未随仓库分发的内部审查报告，且该写法分布于 `backend/`、`src/`、`tests/`、`web/`
  共 **39 个文件**（按 `🔴|🟠|🟡|🟢|P0-x|P1-x|P2-x` 精确匹配统计，已排除 CSS 十六进制颜色
  一类误匹配）。此项属独立的注释清理工作，与文档/配置重写不同层，不影响功能与运行。

- **版本号保持 `2.4.4-fix30`**：它是合法的 SemVer 预发布标识（优先级高于 `2.4.4`），且与历史
  `fix21~fix30` 命名连续。若将来改为 `2.4.4+fix30`，注意构建元数据不参与版本优先级比较，
  语义会弱于预发布标识。版本号三处（`src/__init__.py`、本文件、README 徽章）当前一致。

- **已知环境依赖（非缺陷）**：`tests/test_server_api.py` 依赖 `TestClient`，其首次请求时
  anyio 会在 Windows 上建立 loopback `socketpair()`。受限沙箱/安全软件若拦截 loopback 套接字，
  会抛 `PermissionError: [WinError 10013]`，表现为该文件首个用例失败。判据：单文件运行应通过
  （`pytest tests/test_server_api.py -q`）；CI 运行在 `ubuntu-latest`，不受影响。
  本机（Python 3.13.14 / Windows 10）实测 `socketpair()`、anyio 阻塞门户、TestClient 均正常，
  全量测试连续多次 100% 通过，未能复现该失败。该说明已同步写入测试文件夹具注释。

## [2.4.4-fix30] - 2026-09-14（交付审查问题闭环）

### 修复（Fixed）

- **🔴 dashboard 端点漏传 `alerts`，「数据总览」页告警警示条恒不显示**
  `web/js/pages.js` 的告警渲染读的是 `/api/page/dashboard` 的 `d.alerts`，但该端点
  返回体从未包含 `alerts` 字段 → 渲染分支恒为假，负荷预测降级、温度超限等告警
  在页面上永远看不到（渲染代码实际成了死代码）。`/api/page/thermal` 因走
  `report_dict()` 一直正常透传，故此前未被发现。
  修复：dashboard 端点补 `"alerts": [str(a) for a in (getattr(rep, "alerts", None) or [])]`，
  与 `report_dict()` 同构。新增 `tests/test_alerts_propagation.py` 覆盖该透传链路
  （此前 `alerts` 字段零测试覆盖）。

- **🟠 `restart_backend.bat` 硬编码开发者本机绝对路径**
  原为 `C:\Users\<开发者>\...\python.exe`，换机必然失败。改为与 `start_web.bat`
  同策略：优先项目内 `.venv`，否则回退 PATH 中的 `python`。

- **🟠 `requirements.txt` / `requirements-dev.txt` 的 `~=` 约束与 `requirements.lock` 冲突**
  `pandas~=2.2`（lock 为 3.0.5）、`xgboost~=2.1`（lock 为 3.4.1）、
  `cryptography~=43.0`（lock 为 49.0.0）、`pytest~=8.3`（lock 为 9.1.1）、
  `httpx~=0.27`（lock 为 0.28.1）五处不满足。已按 lock 实测版本修正下界，
  换机 `pip install -r requirements.txt` 与 `-r requirements.lock` 现在口径一致。

- **🟡 CHANGELOG 出现两个 `[2.4.2] - 2026-09-10` 且顺序错位**
  同日本是两批独立修复，编号重复。后者按批次重命名为 `[2.4.2-b]` 并加编号说明。

- **🟡 README 量化成果表与当前代码实测不一致，年化口径混用**
  表中数字为 2026-09-07 记录，与 fix29 实测存在 3~5% 偏差（净收益 1245.30 → 实测
  1198.12 元/日、最高温 44.6 → 46.99℃ 等）；年化收益此前只给一个数字且未说明 DR
  按 50 天/年计入。已按实测数据更新表格，并把年化拆为三个口径并列说明。

### 变更（Changed）

- 删除交付包内的孤儿文件与备份文件（未被任何代码引用，共约 2.67 MB）：
  `tests/fixtures/login-ok.png`、`tests/fixtures/upload_test_730d.csv`、
  `src/agents/chat_agent.py.bak_20260911_181900`。

### 新增（Added）

- `LICENSE`（MIT）——此前声明开源但无许可证文件。
- `tests/test_alerts_propagation.py`——告警透传链路专项测试。

### 验证（Verified）

- 全量 pytest（含 slow）通过，无回归。
- 从零启动冒烟：`/api/page/dashboard` 返回体含 `alerts` 字段。

- **🟡 Python 版本口径三处不一致**
  此前：`.python-version` 声明 `3.11`、CI 用 `3.11`、`requirements.lock` 却在
  Python 3.13 上生成、实际验证环境为 3.13。现统一为 **3.13**（唯一被实测过的版本）：
  `.python-version` 与 CI 的 `python-version` 均改为 3.13，并在 CI 加注释说明
  3.11 下的兼容性本次未验证。`requirements.lock` 的注释同步改为真实环境
  （Python 3.13.14 / 快测 70 项 / 全量 92 项）。

### 已知未处理（纳入技术债迭代）

- 源码注释中 10+ 处 `app.py` 引用为历史沿革，保留并在 README 加说明，不做机械替换。
  （待补：`docs/DEPLOY.md`、`docs/OPS.md`、`/api/health`、多用户与审计日志。）

## [2.4.3-fix29] - 2026-09-14（版本号漂移清理）

### 修复（Fixed）

- **🟠 欢迎页与 README 的版本号硬编码，发版后不随版本更新**
  项目 P0-F1 已确立「版本号单一事实来源 = `src/__init__.py` 的 `__version__`」，
  侧栏副标题也已改为从 `/api/bootstrap` 动态渲染，但仍有两处漏网：
  - `web/index.html` 欢迎页底部硬编码 `汐储 TideShift v2.4.3`（发版后不更新）；
  - `README.md` 标题与版本行停留在 `v2.4.3 / 2026-09-10`。
  修复：欢迎页改为 `<span id="wl-version">` 占位、由 `app.js` 在 boot 阶段从
  `State.boot.version` 填充；README 同步至当前版本，并补一段 fix23–fix28 修复摘要。

### 变更（Changed）

- README 版本行更新为 `v2.4.3-fix29 | 发布 2026-09-14`。

### 验证（Verified）

- 交付包内 `web/index.html` 已无 `v2.4.3` 字面量（仅保留 `?v=fix29` 资源指纹）；
  `src/__init__.py`、`README.md`、`CHANGELOG.md` 三处版本号一致。
- 交付包 `储能调度系统_v2.4.3-fix29_20260914.zip`：68 个文件 / 1.07 MB；
  zip 内路径全部为正斜杠（跨平台可解压）；`config/`（Fernet / JWT 密钥、口令哈希）
  已按 `.gitignore` 的 P0-01 约定排除，不带任何密钥与运行时状态。

## [2.4.3-fix28] - 2026-09-11（弹窗未居中修复）

### 修复（Fixed）

- **🔴 弹窗贴在视口左上角，未居中**
  第 75 行的全局 reset `* { box-sizing: border-box; margin: 0; padding: 0; }` 虽然特异性为 0，
  但**作者样式优先级高于浏览器 UA 样式**——它覆盖掉了 UA 为 `<dialog>` 预设的 `margin: auto`，
  弹窗因此失去居中，固定贴在视口左上角（上传弹窗、确认弹窗均受影响）。
  修复：在 `dialog` 规则中显式恢复 `margin: auto`；同时补 `max-height: 88vh` +
  `overflow: auto`，避免小窗口 / 窄屏下弹窗高度超出屏幕导致底部按钮不可达。

### 验证（Verified）

- 真机实测（1440×940 视口）：弹窗 580×477，几何中心 `(720, 470)` 与视口中心 `(720, 470)`
  **偏差 0px**；`margin` 计算值恢复为 `231.7px 430px`（由 `auto` 解析所得）。
- 该规则对全部 `<dialog>` 生效，JS 动态创建的 `ui-confirm` 等弹窗同样居中。

## [2.4.3-fix27] - 2026-09-11（上传负荷数据弹窗：信息层级与可用性优化）

### 优化（Changed）

- **弹窗宽度 680px → 580px**：原宽度下三列格式卡片占不满、右侧大片留白，内容头重脚轻。
- **拖拽区图标改用内联 SVG**：原为 `📄` emoji（`font-size:22px`），在部分环境下会被渲染成
  异常放大的字形（真机截图已复现该问题）。现为 44×44 圆角图标块，hover / 拖拽时反色高亮。
- **补齐文件约束提示**：拖拽区新增副标题「支持 .csv · 单文件 ≤ 50 MB · 至少 96 条 · 时间戳升序」，
  与 `backend/server.py` 的 `_UPLOAD_LIMIT_BYTES`（50MB）、最少 96 点、
  `is_monotonic_increasing` 校验逐条对齐；此前只有一句"点击选择或拖拽…"，用户无法预知约束。
- **新增「选择文件」主按钮**：原先整个拖拽区可点但没有任何 CTA 提示。该按钮为纯视觉元素
  （`pointer-events:none`），点击统一由外层 `.dropzone` 处理，避免重复触发文件选择；
  键盘可达性仍由 `.dropzone` 的 `role="button"` + `tabindex` + Enter/Space 承担。
- **新增「下载标准格式模板」**：前端直接生成 CSV（96 点 · 15 分钟粒度，字段
  `timestamp,load_kw,price,temp`），带 UTF-8 BOM 防 Excel 打开中文乱码；零后端依赖。
- **格式说明卡片重做**：加序号徽标（1/2/3）、标题与说明分层、字段名用行内 `code` 等宽化——
  原先三种格式挤在一句裸文本里，差异不易扫读。
- **上传结果区分状态**：`#upload-result` 由 `.caption` 改为 `.upload-msg`，新增
  `.busy / .ok / .err` 三态配色；空内容时 `:empty` 自动收起占位，弹窗不再有隐形空行。
- **危险操作位置调整**：「清除当前数据」从全宽主按钮移到右下角次要位置
  （`btn-sm` + `btn-danger-ghost`），不再与上传主流程争夺视觉焦点。
- 拖拽态新增「松开即可上传」文案反馈（CSS `::after`）。

### 验证（Verified）

- 弹窗实测尺寸 **580×445**；拖拽态 `.dropzone` 正确附加 `drag` 类并显示提示文案。
- 样式实测：序号徽标 15×15、格式卡左内距 30px、字段 `code` 有底色、拖拽图标 44×44、
  主标题 13px、CTA `pointer-events:none`。
- **模板闭环实测**：下载 `load_template.csv`（97 行 = 1 表头 + 96 点，UTF-8 BOM 存在，
  首行 `2024-01-01 00:00:00,1020,0.32,19.1`，末行 `2024-01-01 23:45:00,992,0.32,20.5`），
  再将该文件走真实上传链路 → 服务端返回 `已加载 96 条数据 · 标准格式 · 1天 · 请点击运行调度`，
  UI 结果区呈 `.ok` 成功态。

## [2.4.3-fix26] - 2026-09-11（侧栏收起压扁主区修复 + 解释卡片 Markdown 渲染）

### 修复（Fixed）

- **🔴 P0 · 收起侧栏会把主内容区压成 0 宽**
  `.sidebar.collapsed` 使用 `display: none` 隐藏，会把侧栏**从 grid 流中摘除**；
  `.app` 原先依赖 grid 自动放置，于是 `<main>` 整体前移一格、落入第一列——而收起态的
  第一列宽恰为 `0px`，主内容区因此被压成 0 宽。
  真机复现（1080px 视口）：`main 844px → 0px`、`#content 844px → 44px`、
  KPI 卡 `393px → 38px`、topbar `844px → 40px`；对话面板开启时同样触发。
  修复：显式锁定三栏所在轨道，不再依赖自动放置——

  ```css
  .app > .sidebar   { grid-column: 1; }
  .app > .main      { grid-column: 2; }
  .app > .chatpanel { grid-column: 3; }
  ```

  任意一栏显示/隐藏都不再影响其他栏的列位置。

- **🟠 P1 · 「AI 决策解释」卡片的 Markdown 源码原样露出**
  与对话气泡属同类问题但漏改：`pages.js` 的 `aiExpanderHTML()` 用 `esc(exp.text)` 纯文本渲染，
  `## 关键决策`、`**00:00–03:15 谷段大额充电**` 等直接显示为源码。
  修复：改用 `renderMarkdown()`；同时把原本限定在气泡内的 Markdown 样式
  （`.bubble .md …`）提升为**通用 `.md` 容器样式**，使对话气泡与解释卡片共用同一套排版规范。

### 验证（Verified）

- **侧栏交互**（Chrome + Playwright，1080 / 1440 两档视口 × 展开/收起/再展开 × 对话面板开/关
  共 12 组测量）：收起态 `main` 与 `#content` 均等于视口宽度（1080/1440），
  `.main` 的 `grid-column-start` 恒为 `2`，展开宽度可逆（236px）。
- **解释卡片 DOM**：`tags = ul,li,strong,h2,ol,p,blockquote`、`rawMarkdownLeak = false`、
  标题 13.8px + 3px 左边条、`.md` 的 `white-space: normal` 正确覆盖 `.ai-text` 的 `pre-wrap`。

## [2.4.3-fix25] - 2026-09-11（对话回答 Markdown 渲染 + 输出排版规范）

### 修复（Fixed）

- **对话回答的 Markdown 源码原样露出**：气泡此前以 `textContent` / `esc()` 纯文本渲染，
  LLM 输出的 `## 标题`、`| 表格 |`、`**加粗**` 全部以源码形式显示（真机截图反馈）。
  根因：前端从未做 Markdown 解析，且 `.bubble` 带 `white-space: pre-wrap` 保留原始换行。

### 新增（Added）

- **`web/js/markdown.js`**——受控子集的轻量渲染器，不引第三方库（CSP 的 script-src 仅放行
  本地脚本，与 echarts 本地分发策略一致）。
  - 安全：**先对整段文本做 HTML 转义，再按白名单标签生成结构**，内容无法注入标签或属性；
    链接仅放行 `http/https` 且加 `rel="noopener noreferrer"`。
  - 容错：流式下语法可能不完整（未闭合代码块、半截表格），一律降级为文本，不抛错、不吞内容。
  - 支持：标题(#~####)、有序/无序列表(含一层嵌套)、表格、代码块、行内代码、加粗、斜体、
    引用、分隔线、链接。
- **气泡内排版规范（CSS）**：`.bubble .md` 统一字号、间距与层级——
  - 标题字号 H1–H4 = 14.5 / 13.8 / 13.2 / 12.8px（逐级递减，不超过面板标题），
    H1–H3 加 3px 强调色左边条，H4 不加；
  - 标题外边距 12px / 6px；列表一级 `disc`、二级 `circle`，缩进 18px / 15px；
  - 表格 12.2px、单元格内距 5px×9px、超宽横向滚动、末列允许换行、表头强调色浅底 + 字重 600；
  - 代码块用输入框底色 + 等宽字体（未闭合时虚线边框提示仍在生成）；行内代码小圆角底色；
  - 引用 3px 细边线 + 次级文字色；段落间距 8px、行高 1.72；容器首末元素去外边距。
- **输出排版规范（system prompt）**：明确要求结构顺序为「结论→关键数据→依据说明→风险/建议」；
  标题从 `##` 起步、不跳级、单次不超过 4 个；列表项以「**关键词**：说明」开头、嵌套最多一层；
  加粗每节 ≤2 处、禁止整段加粗；表格仅在对比 ≥3 个对象时使用且列数 ≤4；公式用行内代码；
  `>` 引用只用于标注系统结论或风险；并显式禁止输出 HTML 标签、禁止 emoji 堆砌标题、
  禁止用「一、二、三」+ 粗体模拟标题。
- 前端静态资源版本串统一升至 `?v=fix25`（含新增的 `js/markdown.js`），避免浏览器取到旧脚本。

### 验证（Verified）

- **渲染器单测（Node）**：真实回答片段、XSS 注入、未闭合代码块、引用/分隔线、
  有序列表 + 行内代码 + 链接——全部通过（无危险标签、无残留 Markdown 标记）。
- **真机 DOM 实测**（Chrome + Playwright，提问「电池参数设置依据」后读取气泡）：
  `tags = p,br,strong,ul,li,code,h2,blockquote,ol`、`rawMarkdownLeak = false`、
  `h2` 字号 13.8px / 左边条 3px / 外边距 12px·6px、列表 `disc`、段落间距 8px、行高 22.0px。
- **表格实测**：4 行 × 3 列、表头 `rgba(22,93,255,.06)` + `font-weight:600`、
  单元格 `padding:5px 9px`、容器 `overflow-x:auto`、单元格 `border-collapse:collapse`。

## [2.4.3-fix24] - 2026-09-11（模型设置 UI 修复 + 流式输出实装）

### 修复（Fixed）

- **API Key 已配置信息被截断**：此前整串塞进输入框 `placeholder`
  （`已配置 sk-****7af9，留空保持不变`），与右侧「显示/隐藏」眼睛按钮互相挤压后被裁切。
  现拆为**独立第二行** `#mp-key-hint`（`.field-hint` 样式），`placeholder` 精简为「留空保持不变」。

- **「设为当前使用」按钮前的竖线**：按钮文案误写为字面 `I 设为当前使用`，
  该字母被渲染成竖线。已改为 `设为当前使用`。

### 新增（Added）

- **流式输出实装**——此前该开关只把 `llm_params.stream` 持久化，后端 `/api/chat` 仍是
  `agent.respond()` 一次性返回，用户拨开开关也不会逐字：
  - `chat_agent.LLMAgent.astream()`：基于 LangGraph `astream(stream_mode="messages")` 逐 token 产出；
    新增 `chunk_text()` 兼容 `str` 与 content-blocks 两种 chunk 形态，工具调用阶段自动跳过。
  - `POST /api/chat/stream`（SSE）：`data: {"delta": ...}` 增量流 + 收尾
    `data: {"done": true, "history": [...]}`；会话对象在进入生成器前捕获为闭包变量
    （`_CURRENT_SESSION` 是 contextvar，在流式生成器执行上下文中不保证可见）。
  - 前端 `sendChat()` 改为 `fetch` + `ReadableStream` 逐块渲染
    （`EventSource` 仅支持 GET，无法携带 JWT 头），助手气泡原地更新。
  - 流式开关**即时生效**：`#lp-stream` 增加 `change` 监听直接落盘，
    此前必须另点「保存推理参数」，用户拨了开关误以为已开启。

### 验证（Verified）

- 真机 SSE 实测：开启流式 → **22 个增量块**，首字 2.14s，总 2.32s；
  关闭 → **1 块**整段返回，开关双向均符合预期。
- 真实浏览器（Chrome + Playwright）对助手气泡长度逐帧采样：
  `5 → 12 → 72 → 151 → 253 → 308 → 390 → 458 → 487`，确认**逐字渲染**。
- 运行时文案核对：`#mp-key-hint = '已配置 sk-****7af9（留空即保持不变）'`、
  按钮文本 `'设为当前使用'`。

## [2.4.3-fix23] - 2026-09-11（真机连通性验证 + 对话/解释双链路修复）

本副本基线为 fix21。本轮应用 fix22 的对话 Agent 代码修复，并新修一处解释层凭据缺陷。
未包含 fix22 的 README 口径校准与前端图表重排（纯文档/展示层变更，另行同步）。

### 修复（Fixed）

- **🔴 P0 · DeepSeek 配好后对话仍走规则模式（模型"无法连接"假象）**
  `chat_agent.LLMAgent` 使用 langchain 旧 API `create_tool_calling_agent` / `AgentExecutor`，
  而实装 `langchain 1.3.14` 已移除该 API → `ImportError`；工厂的 `except Exception`
  又**静默回退** `RuleBasedAgent`，无任何日志。表现为：Key 有效、网络通畅、依赖齐全，
  但对话永远返回固定兜底文案。
  修复：迁移到 langchain 1.x 新 API `create_agent`（LangGraph 内核），`respond` 读
  `messages[-1].content`；异常区分为 `ImportError` / 其他并打 WARNING，**不再无痕降级**；
  `ChatOpenAI` 显式 `timeout=60 / max_retries=1`。
  `requirements.txt` 补 `langchain-openai~=1.0`，`requirements.lock` 补
  `langchain-openai==1.4.1`（此前未声明，换机重装会再次触发同样的静默降级）。

- **🟠 P1 · Web 界面配置的 Key 对「LLM 决策解释层」不生效**
  `langgraph_coordinator.explanation_node` 以 `LLMExplainer()` **无参构造**，而
  `resolve_llm_credentials` 只支持「显式传参 > 环境变量」——服务进程内并无
  `DEEPSEEK_API_KEY` 环境变量。结果：对话 Agent 已走 LLM，调度内嵌的决策解释却长期
  降级规则模板（日志持续 `source=rule`）。
  修复：`llm_explainer` 新增进程级默认凭据 `set_default_credentials()`，优先级置于
  **显式传参与环境变量之间**（不污染 `os.environ`，可清空）；`backend/server.py` 在
  加载 / 保存模型配置后同步注入。

### 验证（Verified）

- 真机端到端（HTTP 层实测，非推测）：
  - `/api/providers/test` → 连接成功（0.8s · deepseek-chat）
  - `/api/chat`「你好」→ 模型生成回复 2.70s（修复前为规则模板兜底文案）
  - `/api/chat`「解释一下今天的调度策略」→ Markdown 表格分析 8.78s
  - 求解日志：`使用规则模板解释（source=rule)` → `LLM 解释生成完毕：31/32 个数字通过事实回查`
- 离线链路：`create_agent()` 返回 `LLMAgent`（修复前 `RuleBasedAgent`）。
- 单测：`tests/test_llm_explainer.py` + `tests/test_chat_agent.py` 共 23 passed。

## [2.4.3-fix21] - 2026-09-11（代码审查报告 P0×6 + P1×12 闭环）

依据《全量代码审查报告（合并版）》逐条核实后修复。18 项 P0+P1 中：
属实修复 15 项、误报排除 2 项（P1-B6 业务测试已存在、P1-F5 前端 chatHistory 由服务端最近 16 条整体替换无增长）、1 项纳入技术债（P1-F1 全局变量模块化，报告建议渐进式迁移）。

### 修复（Fixed）

**后端安全（P0）**

- **P0-B1 · 登录限流字典无界增长（内存 DoS）**：`_LOGIN_FAILS` / `_LOGIN_LOCKED_UNTIL`
  此前无任何清理机制，攻击者用 1 个 IP + 海量不同 username 可把内存灌到 GB 级。
  现在条目数上限 10000（按插入序淘汰最旧项）+ 后台 sweeper 线程每 5 分钟清理过期锁定记录。
- **P0-B2 · StaticFiles 全目录挂载**：新增 `_GuardedStaticFiles` 扩展名白名单
  （.html/.css/.js/.svg/.png/.ico/.woff2 等），非白名单 403；lifespan 启动时扫描 web/
  目录，发现 .json/.pkl/.py/.env 等敏感类型文件即告警——默认安全，部署误操作不再静默暴露。
- **P0-B3 · 补 HSTS**：CSP 已在先前批次落地（本次顺带移除 script-src 中已无用的
  jsdelivr CDN 白名单）；补 `Strict-Transport-Security: max-age=31536000; includeSubDomains`。

**后端健壮性（P1）**

- **P1-B1 · 上传内存峰值 100MB → 1MB**：分块写入临时文件 + `pd.read_csv(path)` 流式解析，
  替代 `b"".join(chunks)` 全量拼接；临时文件 finally 无条件清理。
- **P1-B2 · DR 触发业务范围校验**：补 target ≤ 10000 kW、subsidy ∈ [0, 20] 元/kWh、
  时段长度 ≤ 8h（此前仅 target>0，curl 绕过前端可传异常值）。
- **P1-B3 · 数据指纹 MD5→SHA256**：截 128 bit（32 hex），消除审计质疑与碰撞隐患。
- **P1-B5 · clear_upload 显式 gc.collect()**：pandas 大 DataFrame 清除后立即归还内存，
  防长期运行"上传-清除"循环内存缓慢增长。

**前端工程（P0/P1）**

- **P0-F1 · 版本号单一事实来源**：`/api/bootstrap` 下发 `version` 字段（源自
  `src/__init__.py`），侧栏副标题动态渲染，消除 v2.4.2 硬编码漂移。
- **P0-F2 · JWT 指纹绑定**：签发 token 时嵌入 User-Agent 指纹（sha256 前 16 hex），
  中间件校验——token 被 XSS 窃取后换环境即失效；无状态实现，旧 token 自然过期淘汰。
- **P0-F3 · 清除上传走统一 API 层**：新增 `API.delete()`，401 时自动弹统一登录层，
  消除裸 fetch 绕过 `_handleResp` 统一错误处理的问题。
- **P1-B4 · ECharts vendor 加 SRI**：`integrity="sha384-…"` 完整性校验，文件被篡改/
  损坏时浏览器拒绝执行。
- **P1-F2 · AI 解释展开器键盘可达**：head 由 div 改为 `<button type="button">`
  （原生 Enter/Space 触发 + aria-expanded），WCAG 2.1 SC 2.1.1 达标。
- **P1-F3 · 页内 tabs 逻辑收敛**：热管理页/需求响应页两处复制的 subtab 切换逻辑
  收敛为 `bindSubTabs(root)` 单一实现。
- **P1-F4 · 删除 priceBands 死代码**：硬编码电价时段且无任何调用，与
  `engineCfg().price_periods` 单一事实来源冲突。

### 已知未处理（评审 P2/P3，纳入技术债迭代）

- P2：JWT jti 黑名单、多 worker 状态外置 Redis、图表实例缓存、轮询动态 setTimeout、
  状态管理分层、CSS 模块拆分、注释清理等 16 项。
- P3：前端单测、ESLint/Prettier、ECharts 按需加载等 4 项。

## [2.4.3-web] - 2026-09-10（Web-only 分发变体）

### 变更（Changed）
- 本包为 **Web-only 变体**：移除 Streamlit 入口 `app.py` 与其依赖（streamlit / plotly / matplotlib），仅保留 FastAPI + 原生 JS 前端（`backend/server.py` + `web/`）。
- 业务层 `src/` 与完整版逐字节一致；`tests/` 全部保留（无一依赖 Streamlit）。
- 启动：`python backend/server.py`（或 `start_web.bat`）→ http://127.0.0.1:8800
- 同步更新 requirements（去掉 3 个不再使用的包）与 README 结构树/技术栈表。

## [2.4.3] - 2026-09-10（上传链路与白屏根因修复）

本轮以「真机复现 → 定位根因 → 收敛实现 → 回归验证」推进，修复 6 项缺陷，其中 3 项为阻断级。
全部结论均由实际运行产生（Streamlit AppTest 无头复现 + Chrome 端到端上传 + 服务端日志对照）。

### 修复（Fixed）

- **P0 · 标准格式上传必定失败**：`adapt_uploaded_data` 的"已是标准格式"分支直接
  `return raw_df` 原样透传，而下游校验要求的是内部字段名
  `price_yuan_per_kwh` / `ambient_temp_c`，UI 与 README 却告诉用户用 `price` / `temp`
  —— 按文档上传 100% 报「缺少必要列」，该入口实际是坏的。
  现在统一做列名归一化（含中英文常见别名），并对缺失的 price/temp 按与另两种格式
  相同的口径补全，同时在弹窗内显式披露"哪一列是自动补全的"。
  *实测：上传文档口径的 2880 行标准文件 → 「已加载 2880 条数据 · 标准格式 · 30天」。*

- **P0 · 上传后页面长时间无响应（表现为卡死/白屏）**：上传数据的电价走的是前端自实现的
  `_default_price_by_hour`，其时段划分与 `PRICE_PERIODS` 不一致，其中含 6 小时连续同价的
  平段，使 MILP 最优解大量退化 —— 实测同一模型，内置电价 7.2s、上传电价 120.1s（撞满
  时限），叠加首屏骨架屏即表现为"卡死"。现将时段划分收敛到单一事实来源（前端只保留价格值
  覆盖能力）。*实测：同一 5 天数据集重跑，MILP 由 120s 降至 33s，页面在 45s 内正常出结果。*

- **P0 · 调度日期控件与真实数据脱钩（换数据即白屏）**：`_resolve_selected_date()` 与
  `render_op_bar()` 对内置数据写死了 `pd.date_range("2024-07-08","2024-07-28")`，
  `server.py` 侧 `default_date()` 写死 `"2024-07-15"`、`/api/bootstrap` 写死同一区间。
  与 `data/load/load_data.csv` 的真实范围（2024-07-01 ~ 07-30）不符：下拉框静默隐藏
  07-01~07-07 / 07-29~07-30；一旦内置 CSV 换成别的日期区间（部署真实数据的常规动作），
  默认日不存在于数据中 → 切出空 `day_data` → 整页内容不再渲染。
  现全部改为从当前生效数据派生（新增 `_available_dates()` / `_default_date_of()`），
  并在控件实例化前清理越界的会话态。

- **P1 · 短数据产出荒谬预测并把求解拖满时限**：滞后特征含 `lag_672`（7 天），历史不足
  8 天时 `dropna` 后训练集为空，XGBoost 仍被强行 `fit`，随后"数据驱动物理修正"因拟合失败
  落入物理公式分支，输出量级失真的修正量。*实测日志：`训练集: 0 条` +
  `物理修正幅度: 27275.8 kW`（正常量级应为数百 kW）。*
  现在：训练样本 < 96 条时显式判定"数据不足"，不训练，降级为朴素基线且不做物理修正，
  降级原因写入 `ForecastResult.fallback_reason`。
  *实测：同一 5 天数据集，修正量归零、预测落在 771~2218 kW 的合理区间。*
  另修 `_physical_correction`：`mode="data_driven"` 未拟合时不再静默换用 `predict_simple`
  的物理公式口径（两者修正强度差一个量级），改为明确不修正。

- **P1 · 告警只生成不消费**：`DailyReport.alerts` 在两个前端均无任何消费点，负荷预测降级、
  DR 事件时间重叠等告警全部静默丢弃，报表上看不出本轮口径已变。
  现在：Streamlit 总览页以 `st.warning` 呈现，`/api/*` 的 `report_dict()` 透传 `alerts`
  并由 Web 总览页渲染警示条；预测异常降级分支也补上了告警（原先只有正常分支写 alerts）。

- **P2 · 两处对外文案与事实不符**
  - 总览页底部残留一条介绍 LangGraph 架构图的说明，但该图已从本列移除 → 悬空引用，
    改为说明本列对比表的口径。
  - 侧栏硬编码「内置演示数据 · 730天」，而随包内置数据实际只有 30 天 → 改为动态统计
    并显示真实区间。

### 变更（Changed）

- **消除双前端重复实现**：新增 `src/data/upload_adapter.py`，把上传格式识别、列名归一化、
  默认分时电价三处逻辑收敛为唯一实现，`app.py` 与 `backend/server.py` 均改为委托调用
  （此前两份实现已实际分叉：标准格式分支一方透传、分时电价两方各写一套时段）。
- **求解缓存键加入电价时段划分规则版本**（`PRICE_RULE_VERSION`）：键里原先只有价格数值、
  不含时段划分，口径调整后旧缓存会被错误复用。版本号做任何一次时段口径调整时必须 +1。

### 验证（Verified）

- 全量 pytest（含 slow）：通过，无回归。
- Chrome 端到端上传（NREL ComStock 5 天 / 标准格式 30 天）：上传 → 确认 → 重算 → 出结果
  全链路无异常、无长时间无响应。
- 修复前后日志对照：`训练集 0 条 + 修正 27275.8kW + 求解 120s` → `显式降级 + 修正 0.0kW + 求解 33s`。

## [2.4.2] - 2026-09-10（方案C：演示数据兜底 + 显式标注 + 欢迎页可选上传）

### 新增（Added）
- **上传弹窗分阶段进度条**：`upload_dialog()` 内以 `st.progress` 替换单行 spinner，
  按「📥 读取文件字节流 → 📖 解析 CSV（显示 MB）→ 🔍 格式识别与字段映射（显示 行×列）
  → ✅ 校验必要字段」四阶段推进，完成后撤条显示 success；缺列/解析失败时清条再报错。
- **欢迎页可选上传入口**：「进入系统」下新增「📂 上传我的数据（可选）」次级按钮 +
  caption「不上传则使用内置演示数据」；点击后经 `_open_upload_after_enter` 标记进入系统，
  主脚本侧自动打开现有 `upload_dialog()`（沿用 `_upload_dialog_closed` 主脚本重跑机制）。
- **演示数据显式标注（3 处）**：新增模块级 `is_demo_data` 标记——侧栏 caption 改为
  「📊 内置演示数据 · 730天 · 上传自有数据后自动切换」；总览页标题下新增
  「当前结果基于内置演示数据（合成负荷）」caption；README 新增「数据说明」小节
  （合成数据来源声明 + 三种上传格式说明）。缓存切换 / 格式识别 / DR 流程零改动。

## [2.4.1] - 2026-09-09（P1 收尾批次，与前端修复批次并行）

后端遗留 P1 三项收尾（工程保障团队核查后修复；仅动后端文件，web/ 由前端修复批次独立处理）。

### 修复（Fixed）
- **#17 print→logging 收尾**：src/agents 与 backend 共 **89 处 print 迁移**至统一 logger
  （AST 语义等价转换：单参数直传、多参数 `print(A,B)` → `" ".join(...)` 保持空格分隔语义；
  含 ⚠/失败/异常 的消息自动升为 WARNING；首启口令提示保留控制台可见性）。
  `data_generator.py` 的 6 处 CLI 输出有意保留（终端工具体验）。
- **#19 app.py 侧 chat_history 环形上限**：新增 `_trim_chat_history()`（上限 200，与 server.py
  侧对齐），两处 append 后裁剪——消除长会话内存缓慢增长。
- **#20 thermal pe 结果磁盘缓存**：`_compute_pure_econ()` 结果落盘（v2 dict 格式 +
  `cache_security` HMAC 签名通道，受磁盘 LRU 50 个/1GB 统一管理）——服务重启后同参数
  免重算 120s MILP；缓存键补数据指纹（fp8），换数据不碰撞。磁盘读写失败均降级重算并告警。
  （完整异步化=后台任务+前端轮询需改 web/pages.js，待前端修复批次合并后在 v2.4.2 处理。）

### 验证（Verified）
- 全部改动 `py_compile` 通过；pytest 快测层 **55/55 passed**（无回归）。
- 迁移后日志真实落盘 `logs/app.log`（UTF-8，RotatingFileHandler 生效）。

### 品牌与前端（Added/Fixed）—— 方案 A「汐储 TideShift」落地 + 前端 UI 评审 P0 修复
- **品牌重塑**：中文名「汐储」、英文名「TideShift」，tagline「观峰如潮，向谷而储」。
  新 logo = 渐变圆角底 + 峰谷波形 + 金色闪电（内联 SVG，零图片依赖）。
  替换点：欢迎页 logo/标题、web 侧栏品牌区、favicon（SVG data-URI）、顶栏标题、
  Streamlit 侧栏标题（顺带修复其版本号仍写 v1.2 的漂移）、README 标题。
- **欢迎页改版**（Streamlit 版）：
  - 修复垂直假居中（margin:auto 原加在每个子块致空隙均分 → 改为首块/末块 auto）
  - 双光斑背景（主题色 radial，浅/深主题自动适配）
  - 6 张卡片一体化（按钮上圆角 + 说明 caption 下圆角贴合，hover 上浮）
  - 技术徽章语义分色（绿/蓝/琥珀/青/粉）
  - 进入按钮胶囊化；新增 footer（运行状态 · 版本 · 技术栈 · tagline）
- **前端 UI 评审 P0 全修**：
  - P0-1 移动端首屏死锁：窄屏初始化强制收起 sidebar + 点遮罩关闭 + topbar 提升至遮罩之上
  - P0-2 移动端聊天面板 0 宽溢出：改右侧 fixed 抽屉（340px/88vw，transition 滑入）
  - P0-3 热管理页电价图例硬编码：改从 `engineCfg().price_periods` 动态渲染，未配置时显示占位不编造
  - P0-4 充放电柱同色：放电改 `p.discharge`
- **评审 P1/P2 快速项**：错误态不再渲染 `e.stack`（改 message + 重试按钮，堆栈进 console）；
  `.btn:disabled`/`:focus-visible` 补样式；登录浮层脱离令牌的 `--bg-root/--radius-md` 改回
  `--bg-page/--radius`；轮询空闲退避（900ms→约4.5s 一次）+ 自动补跑指数退避上限 3 次（防重试风暴）；
  ECharts 1.03MB vendor 本地化（`web/vendor/`，内网/离线可用，去除 jsdelivr CDN 依赖）；
  web 侧栏版本号 v2.3.1 → v2.4.1。
- 验证：4 个 JS `node --check` 通过、app.py 编译通过、快测层 55/55 全绿。

## [2.4.2-b] - 2026-09-10（运行时修复批次：执行流图 / 上传弹窗 / 缓存自愈 / 演示开关）

> 编号说明：与上一节 `2.4.2` 同为本日两批独立修复，原文档两节编号重复，此处按批次拆分为 `-b`。

以下条目均为 v2.4.1 zip 打包后、实际运行验证阶段发现并修复的问题。

- **修复：概览页 TypeError 假死（坏磁盘缓存 + margin 参数冲突）**：
  1. 进入总览页命中 `.solve_cache` 旧坏条目（viz=None/旧结构，缺 time_index），
     恢复成 `_CachedCoordinator(None)` 后在 `viz["time_index"]` 处 TypeError。
     修复：`_load_disk()` 增加 viz 合法性校验（必须为 dict 且 time_index 非空），
     坏条目按未命中处理触发真实重算；渲染前增加兜底自愈（清 session + 内存缓存后重跑一次，
     `_viz_rescue_rerun` 防循环）。
  2. LangGraph 执行流图 `update_layout(..., margin=..., **PLOT_COMMON)` 与 PLOT_COMMON
     内置 margin 键冲突 → `got multiple values for keyword argument 'margin'`。
     修复：margin 移出首次调用，按项目既有模式（fig_sched 同款）二次 `update_layout` 覆盖。
     已离屏 exec 图代码块验证通过。
- **新增：登录门演示模式开关**：`ENERGY_DEMO_NO_LOGIN=1` 环境变量可跳过 Streamlit 登录门
  （`_require_login()` 入口短路，返回 `{"sub":"admin"}`），用于本地演示/内网试用。
  安全整改 🔴#2 逻辑不变：默认（不设变量）仍强制登录，正式部署禁止开启。
- **修复：上传弹窗出现两个「浏览文件」按钮（CSS 选择器过宽）**：
  file_uploader 中文化规则误用 `[data-testid="stFileUploader"] button` 无差别命中 uploader 内
  全部按钮。Streamlit ≥1.48 选中文件后文件行还渲染 ×删除（stBaseButton-minimal）与
  +添加（stBaseButton-borderlessIcon），被 `font-size:0 + ::after"浏览文件"` 伪装成第二个
  「浏览文件」胶囊，且真实删除入口被藏掉。收窄为
  `[data-testid="stFileUploaderDropzone"] button:not(...minimal):not(...borderlessIcon)`
  （浅色主题覆盖同步收窄）；× / + 恢复原图标，主上传按钮中文化不受影响。
  已用无头浏览器对 Streamlit 1.60 真实 DOM 双态（未选/已选）验证：选中态两个 chip 按钮
  `::after: none`，未选态主按钮唯一「浏览文件」。
- **概览页架构图重画（去虚构拓扑）**：原「调度协调Agent 星型分发」Plotly 散点图与真实实现不符
  （真实系统无独立协调 Agent，各节点由 langgraph_coordinator 的 StateGraph 串排）。
  重画为与 `src/agents/langgraph_coordinator.py: build_scheduling_graph()` 完全一致的
  LangGraph 执行流：START → init → load_forecast → storage_optimization → dr_handler
  ⇄（条件边自循环：还有DR事件）→ finalize → explanation → END；边上标注传递数据
  （参数·电价 / 预测负荷曲线 / MILP调度表 / DR执行结果 / 汇总报告），节点 hover 显示真实节点 ID，
  图下 caption 注明与源码一致性。

## [2.4.0] - 2026-09-09

全面代码审核整改（对应《储能调度系统 v2.3.1 全面代码审核报告》50 项发现）。

### 修复（Fixed）——🔴 严重 9 项
- **密钥分发**：补 `.gitignore`（config/、.solve_cache/、*.pkl、.env 等）；删除随包分发的 `.solve_cache/.cache_secret` 与缓存文件（密钥轮换：重启自动生成新密钥，旧缓存全部失效）
- **Streamlit 裸奔分叉**：app.py 接入登录门（共用 backend/auth.py 的 AuthStore + JWT）；模型配置读写统一走 `src/services/model_config_service.py`（api_key Fernet 加密落盘、读盘解密、输入框不回填、留空保留原 Key）；`_test_llm_connection` 接入 SSRF 防护
- **默认口令**：首启生成随机强口令打印控制台（可 `ADMIN_INITIAL_PASSWORD` 覆盖），`admin/admin123` 消失；`must_change=true` 时中间件强制拦截（仅放行改密接口）
- **全局单例会话串扰**：AppState 按 JWT sub 拆分（contextvar 注入 + TTL 12h + 100 会话上限 + LRU 淘汰），chat 回写纳入会话锁；未认证上下文回退默认实例
- **MILP 状态不校验**：`optimize()` 求解后立即校验——Infeasible/无可行解显式抛错；超时拿到可行解降级为次优解并置 `time_limit_hit`；`soc_values` 提取补 `or 0.0`（原全函数唯一缺失处，None 会导致 object 数组）；`solver_status/time_limit_hit/mip_gap_pct` 透传 `report_dict`，前端显著标注"次优解"
- **负荷预测数据泄漏**：`predict()` 先剔除预测日再训练 + "预测日不在训练集"断言——MAPE 回归泛化误差
- **安全模块零测试**：新增 test_auth_security / test_cache_security / test_server_api（JWT 篡改/alg 混淆/过期、篡改缓存拒绝、must_change 拦截、登录限流、SSRF 黑名单、会话 401）
- **相对路径 import 崩溃**：data_loader 默认数据目录改 `Path(__file__).parents[2]/"data"`，任意 CWD 可启动
- **上传 DoS**：Content-Length 预判 + 1MB 分块流式读，累计超限即断

### 修复（Fixed）——🟠 高 17 项（节选）
- 缓存密钥移出缓存目录（config/.cache_secret，可 `ENERGY_CACHE_SECRET_FILE` 覆盖）；pickle 换受限 Unpickler（模块白名单）；磁盘缓存 mtime LRU（50 个/1GB）；密钥生成加 O_EXCL 锁（P0-47）
- 缓存指纹改 pandas 内容哈希（删除 id() 分支）；伪造数据治理：删除硬编码"15:45 驳回"日志、误差直方图无实际数据时返回空 + `error_hist_synthetic` 标注
- DR/负荷预测 Agent 统一 `active_config()`（修复配置分叉）；衰减模型/协调器 clip 边界改 soc_min/soc_max 并统计越界步数；finalize 补传 `energy_balance_error_kwh`
- `highspy` 补入依赖，HiGHS 回退 CBC 必打 WARNING；依赖 `~=` 兼容 pin + `requirements.lock` + `.python-version`
- 接入 logging + RotatingFileHandler（logs/app.log），求解异常 `exc_info=True` 落盘
- 登录限流（IP+账号，5 次失败指数退避锁定）；`compare_digest` 前 encode 修复中文用户名 500
- 双前端供应商配置抽 `src/services/` 共享服务层（11 处重复中最安全关键的一组）；安全响应头中间件（CSP/XFO/nosniff）
- thermal 页 single-flight 锁；`_SOLVE_MEMO`/`_SOLVED_KEYS`/chat_history 加上限；删除 `_SOLVE_REGISTRY` 死代码

### 修复（Fixed）——🟡/🟢（节选）
- 热模型 off-by-one：末步温度纳入最高温统计、产热与本步功率对齐；`estimate_temperature_rise` 充电符号与 `compute_heat_generation` 自洽
- big-M 按 mid/thr 值域收紧（M=2000 → ~0.7/500）；DR 硬约束超额定功率钳制告警；全平电价零除保护；衰减模型 NaN SOC/零 DoD 显式报错
- 魔法数字注释/配置化（SOC 能量折算取配置容量、58=14:30、DR 年化系数）；`dark-dashboard` 补 .png 扩展名；散落文件归位 tests/fixtures/；补齐 6 个 `__init__.py`；README 结构树重写

### 新增（Added）
- 测试基础设施：pytest.ini（slow 标记，PR 快测/nightly 全量）、conftest.py、requirements-dev.txt、GitHub Actions CI
- 新测试 5 个文件 22 项：auth/cache/server 安全 + ML 生产默认路径（含泄漏防护自检）+ 数值边界
- `.env.example`（ENERGY_HOST/PORT/LOG_LEVEL、密钥与 SSRF 白名单变量）；host/port 环境变量化

## [2.3.1] - 2026-09-09

欢迎页居中与顶栏图标贴边。

### 修复（Fixed）
- 欢迎页内容改为视口正中央：welcome-mode 下主容器 flex 化，子块 margin-block:auto 垂直居中（720p 实测中点 360=360）；内容超高（如 640x480）时 auto 归零自然回退顶部并保留 24px 呼吸留白，可正常滚动，任何窗口尺寸不错位
- 对话栏展开时顶栏三个图标（调度助手/主题/收起）紧贴面板左缘（间距 4px）：删除与主容器避让叠加的二次 margin-right:404px（此前图标组被推到页签旁，与面板之间空出 424px）；删除后页签反而全部可见

## [2.3.0] - 2026-09-09

欢迎页与默认主题。

### 新增（Added）
- 欢迎页：每个会话首次访问展示，含项目名称、简要介绍（技术栈徽章）与 6 个功能模块直达入口（点击卡片切换到对应页面）+ 进入系统按钮；进入后本会话不再显示
- 欢迎页独立注入主题属性（位于侧栏 JS 之前 stop，避免主题变量落到深色 fallback）

### 变更（Changed）
- **默认主题改为浅色模式**（_theme_css / data-theme / 设置页恢复默认 / web 版 index.html 与 api.js 共 6 处默认值统一），顶栏按钮与设置页的主题切换功能保持不变

## [2.2.2] - 2026-09-09

顶栏布局重构。

### 变更（Changed）
- 左右边栏收起按钮全部移入中间区 top 栏：侧栏按钮在 logo 之前（最左），对话栏按钮在最右（主题按钮之后），四角悬浮按钮消失
- 右侧对话栏与左边栏等高：top 从 107px（导航栏底边）改为 0，与左侧栏同为全视口高
- 对话栏展开时 top 栏自适应：右侧按钮组避让 416px，logo/标题/运行中暂时隐藏，页签用 safe center + 横向滚动保证 1080 视口可用；收起后恢复完整顶栏

## [2.2.1] - 2026-09-09

UI 精简版。

### 移除（Removed）
- 求解成功后的"✅ 调度完成"状态条：st.status 包进 st.empty 宿主，求解成功即整体移除（失败时保留以便查看错误上下文）
- 缓存命中的"✅ 命中结果缓存…"说明文字与"⚡ 命中结果缓存，秒开"toast（缓存机制本身不变）
- 设置页"系统信息"区块（含陈旧 v1.0.0 版本行）；web 版同步移除对应区块与 /api/system-info 拉取

## [2.2.0] - 2026-09-09

安全与一致性加固版（对应《代码审计问题清单》第二批、第三批）。

### 安全（Security）
- **P0-02** 全部 `/api/*` 接口启用 JWT 认证（HS256，标准库实现，12h 过期）：新增 `backend/auth.py`，HTTP 中间件统一拦截，静态资源与 `/api/auth/login`、`/api/system-info` 豁免；前端登录浮层（localStorage 持久化 token，401 自动重弹）；账户 `config/auth.json`（PBKDF2-SHA256 200k 迭代），首次启动自动创建 admin/admin123 并提示改密，`/api/auth/change-password` 支持修改
- **P0-02** LLM API Key 落盘加密：`config/model_providers.json` 仅存 `api_key_enc`（Fernet，密钥 `config/.api_secret` 0600），内存与运行时不受影响；`GET /api/providers` 脱敏回传（`has_key` + `api_key_masked`），保存时空 Key 按供应商 id 保留原密钥，测试连接支持脱敏回退
- **P0-04** CONFIG 不可变化：4 个配置 dataclass 改 `frozen=True`，求解时经 `replace()` 构建快照并注入 `use_config()`（线程隔离 thread-local），5 处全局赋值点全部移除，并发求解参数互不污染
- **P1-05** 上传校验：50MB 上限、时间戳可解析且升序、最少 96 点、负荷/电价/温度数值范围检查，违规返回 400 与明确提示
- **P1-06** `/api/params` Pydantic 字段校验：SOC/额定功率/电价/XGBoost 超参范围与 `soc_min < soc_max` 组合校验，越界返回 422（前端 detail 已格式化为可读消息）
- **P1-07** DR 重叠防护：手动触发时校验 HH:MM 格式/时间顺序/目标为正，与已有事件重叠返回 409 与重叠明细，前端确认后 `force=true` 强制叠加
- **P1-10** 前端 XSS 审计补漏：`pr.id`、DR 事件时段/历史、对比表策略名等 5 处动态插值补 `esc()`；对话/错误栈/模型名此前已转义

### 修复（Fixed）
- **P1-13** DR 热安全校验全天化：`_check_thermal_safety` 由仅校验 DR 窗口改为取全天温度包络最大值，捕捉 DR 结束后热惯性超调（τ≈4.2h），新增 `window_max_temp`/`overshoot_note` 字段与 `tests/test_dr_thermal_overshoot.py`（2 例）
- **P1-14/P1-15** 展示同源化：前端（web）热参数与电价时段改为读取 `/api/bootstrap` 下发的 `engine_config`（来自 CONFIG/PRICE_PERIODS 单一来源）；app.py 电池页/设置页硬编码的陈旧热参数（65 kJ/K/0.035 K/W/0.8 mΩ，与真实值 15000/0.001/18 不符）与过时电价时段全部改为 CONFIG/PRICE_PERIODS 渲染

### 验证
- pytest 47/47 通过（8m16s，含 2 个新增热安全测试）
- 真机实测：未登录 401 / 登录 200 / Key 落盘密文无明文 / GET 脱敏 / 上传 50 行拒 96 行过 / 参数越界 422 / DR 重叠 409→force 200 / 设置页热参数与电价时段与引擎一致

## [2.1.0] - 2026-09-09

安全加固版（对应《代码审计问题清单》"立即修复"批次）。

### 安全（Security）
- **P0-01** 磁盘求解缓存改为 HMAC-SHA256 签名读写（新增 `src/utils/cache_security.py`，server.py 与 app.py 共用），篡改/投毒的缓存文件验签失败按未命中处理，消除 pickle 反序列化任意代码执行风险
- **P0-03** `/api/providers/test` 增加 SSRF 防护：仅允许 http/https 与 80/443 端口，拒绝内网/环回/链路本地/元数据地址；放行名单可用环境变量 `LLM_TEST_ALLOW_HOSTS` 扩展

### 修复（Fixed）
- **P1-11** `requirements.txt` 补齐 FastAPI 运行依赖（fastapi / uvicorn / python-multipart / pydantic），全新环境一条命令可启动
- **P1-12** `start_web.bat` 去除开发者个人路径，改用 PATH 中的 python；新增 `start_web.sh`

### 变更（Changed）
- **P1-01** 版本标识统一：`src/__init__.py` 的 `__version__ = "2.1.0"` 为唯一来源，`/api/system-info`、前端品牌栏、README 同步读取/更新
- 新增本 `CHANGELOG.md`

### UI（v1.2.4，同日早前）
- 全站 UI 巡检 11 项修复：对比度（H2/H3）、电池页 tab 图表零尺寸（H1）、冷启动缓存重放崩溃（H4）、间距/控件/字号令牌化（M1/M2/M3）、空态（M4）、图标溢出（L1）、禁用态（L2）、图表小字（L3）

## [1.2.0] - 2026-09-08

- LLM 决策解释层（`src/agents/llm_explainer.py`）：事实摘要 + 数字回查 + 降级链
- LangGraph 图新增 `explanation` 节点
- 顶部真实 tab 导航 + 求解进度条

## [1.0.0]

- 首个公开版本：MILP 峰谷套利 + 需求响应 + RC 热模型 + SOC 加权寿命衰减 + Streamlit 前端

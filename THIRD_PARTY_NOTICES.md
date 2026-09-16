# 第三方组件与许可声明

本仓库（**汐储 TideShift**）自身以 **MIT 许可证**发布，见 [`LICENSE`](LICENSE)。

本文档列出**随仓库一起分发**的第三方组件及其许可，以及不随仓库分发的依赖如何处理。

---

## 一、随仓库分发的组件

### 1. Apache ECharts 5.6.0

| 项 | 内容 |
|---|---|
| 文件 | `web/vendor/echarts.min.js`（本地分发，未修改上游代码） |
| 许可 | Apache License 2.0 |
| 版权 | Copyright 2017-2024 The Apache Software Foundation |
| 上游 | https://github.com/apache/echarts |
| 许可原文 | [`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt) |
| 上游 NOTICE | [`licenses/echarts-NOTICE.txt`](licenses/echarts-NOTICE.txt) |

> Apache-2.0 第 4(d) 条要求：分发衍生作品时须包含上游 NOTICE 文件中的归属声明。本仓库通过
> `licenses/echarts-NOTICE.txt` 满足该要求，同时保留 `echarts.min.js` 文件头部的原始许可横幅。

### 2. ECharts 内嵌的上游组件

`echarts.min.js` 是打包产物，其内部还含以下上游代码。这些组件的许可横幅均**原样保留**在该文件内：

| 内嵌组件 | 版权 | 许可 | 说明 |
|---|---|---|---|
| **ZRender**（高性能 2D 绘图库） | Copyright (c) 2017, Baidu Inc. | **BSD 3-Clause** | 许可原文见 [`licenses/zrender-BSD-3-Clause.txt`](licenses/zrender-BSD-3-Clause.txt) |
| **Microsoft Corporation** 代码片段 | Copyright (c) Microsoft Corporation | **0BSD**（Zero-Clause BSD） | 许可文本即以 `Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted.` 开头，直接内嵌于文件内 |

> 注意：ZRender 采用 **BSD 3-Clause**，与 ECharts 自身的 Apache-2.0 **不是同一许可**。
> 两者均为宽松许可、允许商用与再分发，但 BSD 3-Clause 禁止使用版权人名义为衍生品背书。

### 3. 本仓库未做的修改

未修改 `web/vendor/echarts.min.js` 的任何字节，仅做本地化分发（使前端在完全离线环境下可用）。
因无修改，不触发 Apache-2.0 第 4(b) 条「修改文件须显著标注」的要求。若后续升级或改动该文件，
必须在本文档中记录版本变更，并保留原始许可与版权横幅。

---

## 二、不随仓库分发的依赖

Python 依赖（`requirements.txt` / `requirements.lock` / `requirements-dev.txt` 中列出的
numpy、pandas、scipy、PuLP、highspy、XGBoost、scikit-learn、LangGraph、LangChain、FastAPI 等）
**不随本仓库分发**，由使用者通过 pip 从 PyPI 安装。它们的许可证随各自发行包提供。

需要生成完整依赖许可清单时：

```bash
pip install pip-licenses
pip-licenses --format=markdown --with-urls
```

---

## 三、维护约定

新增或更新 `web/vendor/`、`licenses/` 下的任何第三方资源时，必须**同时更新本文件**，并确保：

1. 原始许可文本与版权声明完整保留；
2. 许可原文归档到 `licenses/`（或注明上游获取地址）；
3. 若上游分发包含 NOTICE 文件，须一并归档。

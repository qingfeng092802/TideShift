"""防幻觉守卫的评测 —— 被测对象是 check_grounding() 本身。

为什么单独测守卫：解释层的整条承诺是"LLM 不许自己造数字，造了会被拦下来"。
这句承诺的可信度取决于守卫的**捕获率**与**误报率**，而两者都要靠主动构造样本才量得出来。
只演示"编一个 9999 元被拦"一个例子，等于没测。
"""
from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Sequence

from evals.metrics import GroundingProbeResult
from src.agents.llm_explainer import check_grounding

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _floats(items: Iterable[str]) -> List[float]:
    out: List[float] = []
    for raw in items:
        m = _NUM_RE.search(str(raw))
        if m:
            out.append(float(m.group(0)))
    return out


def _covered(value: float, pool: Sequence[float], rel_tol: float = 1e-6) -> bool:
    return any(abs(value - p) <= abs(p) * rel_tol + 1e-9 for p in pool)


def render_probe(probe: Dict[str, Any], digest: Any) -> str:
    """用事实摘要的真实字段渲染回答模板。

    真数字一律由摘要渲染而来，不写死在 YAML 里：否则改了调度结果，
    "误报率"会在一个已经过期的答案上测出假绿。
    """
    return str(probe.get("answer_template", "")).format(**asdict(digest))


def run_probe(probe: Dict[str, Any], digest: Any) -> GroundingProbeResult:
    answer = render_probe(probe, digest)
    report = check_grounding(answer, digest)
    flagged = list(report.unknown)
    flagged_nums = _floats(flagged)
    planted_nums = [float(x) for x in probe.get("planted_numbers") or []]

    missed = [f"{v:g}" for v in planted_nums if not _covered(v, flagged_nums)]
    # 被标注但不属于埋入值的，即为误报（真数字被当成编造）。
    #
    # 逐条就地取数，不再 zip(flagged, flagged_nums)：`_floats` 会丢掉**没有数字**的
    # 条目（例如"未匹配到"这类纯文字告警），于是两个列表长度可以不等，
    # zip 会把第 1 条的数字配到第 0 条上，误报清单整体错位。
    # 这种错位不会报错，只会让"误报率"这个指标悄悄失真 —— 而它正是本文件的被测对象。
    false_pos: List[str] = []
    for f in flagged:
        nums = _floats([f])
        if nums and not _covered(nums[0], planted_nums):
            false_pos.append(f)

    return GroundingProbeResult(
        probe_id=str(probe.get("id", "?")),
        planted=[f"{v:g}" for v in planted_nums],
        expected=[x for x in _floats(_NUM_RE.findall(answer))
                  if not _covered(x, planted_nums)][:8],
        flagged=flagged,
        missed=missed,
        false_positives=false_pos,
    )


def run_probes(digest: Any, probes: Iterable[Dict[str, Any]]) -> List[GroundingProbeResult]:
    return [run_probe(p, digest) for p in probes]

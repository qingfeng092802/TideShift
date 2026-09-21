"""基本电费（容（需）量电价）计费与整月需量峰值跟踪。

为什么单开一个模块：本项目的账此前只算了**度电能量成本**（TOU 电价 × 功率），
而两部制用户账单里还有一块与用电量无关的**基本电费**——按整月的最大需量
（或变压器容量）计收。漏了它，"储能帮用户省了多少电费"这个问题就只答了一半，
而且漏的那一半恰好是储能最擅长的（削峰值）与最不擅长的（省电费）分界所在。

这一轮**只做事后计费，不进 MILP 目标函数**：先拿真实规则算清楚
"现有的价格优化调度，在基本电费上到底动没动数字"，再决定要不要闭环。

规则与单价出处（逐条对应见 `data/README.md`「基本电价」一节）：

- 《国家发展改革委办公厅关于完善两部制电价用户基本电价执行方式的通知》
  发改办价格〔2016〕1583号（2016-06-30）：基本电价按变压器容量或按最大需量计费，
  **由用户选择**，可提前 15 个工作日按季变更；选按最大需量的须与电网企业签合同、
  **按合同最大需量计收**；实际最大需量**超过合同确定值 105% 时，超过部分加一倍收取**，
  **未超过 105% 时按合同确定值收取**；申请核定值低于（变压器容量＋高压电动机容量）
  总和 40% 的，按 40% 核定。合同核定值可提前 5 个工作日按月变更。
- 单价：广东省电网企业代理购电工商业用户电价表（惠州市，执行时间 2026 年 2 月）
  两部制 1–10（20）千伏栏——最大需量 36.1 元/千瓦·月、变压器容量 22.6 元/千伏安·月。
  同一张表里单一制用户这两栏是"/"，即不收基本电费，所以下面要能表达 `mode="none"`。

所有单价与阈值一律从 `config.DemandTariffConfig` 读，本模块不抄第二份数字
（`tests/test_demand_charge.py` 里有守这条的断言）。
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np

from src.utils.config import CONFIG, DemandTariffConfig

MODES = ("none", "capacity", "contract", "actual")


@dataclass(frozen=True)
class BasicCharge:
    """一个月的基本电费结果（单位：元/月）。"""

    mode: str
    peak_kw: float            # 该月实际最大需量（电网侧取电峰值）
    contract_kw: float        # 合同核定值（含 40% 容量下限）；非合同制为 nan
    band_kw: float            # 触发加价的阈值 = 105%×核定值；非合同制为 nan
    over_band_kw: float       # 超过阈值的千瓦数
    charge_yuan: float


def effective_contract_kw(cfg: DemandTariffConfig) -> float:
    """合同核定值的实际生效值：不得低于变压器容量的 40%（1583号）。"""
    floor = cfg.min_contract_capacity_ratio * cfg.transformer_kva
    return max(cfg.contract_demand_kw, floor)


def basic_charge(peak_kw: float, *, cfg: DemandTariffConfig,
                 monthly_energy_kwh: Optional[float] = None) -> BasicCharge:
    """按计费方式算一个月的基本电费。

    `monthly_energy_kwh` 只用于 `mode="none"` 时保持字段一致，不参与任何计费。
    落不到任何一种计费方式一律抛错，不静默按 0 元——那是本仓库对"改漏了"的固定态度。
    """
    p_kw = cfg.demand_price_yuan_per_kw_month
    p_kva = cfg.capacity_price_yuan_per_kva_month

    if cfg.mode == "none":
        return BasicCharge(cfg.mode, peak_kw, math.nan, math.nan, 0.0, 0.0)

    if cfg.mode == "capacity":
        # 按变压器容量：与月峰无关，削峰一分省不下来（这就是"由用户选择"要比较的东西）
        return BasicCharge(cfg.mode, peak_kw, math.nan, math.nan, 0.0,
                           cfg.transformer_kva * p_kva)

    if cfg.mode == "actual":
        # 按实际最大需量：线性，每一 kW 月峰都值钱。这是 2016 年之前的老口径。
        return BasicCharge(cfg.mode, peak_kw, math.nan, math.nan, 0.0, peak_kw * p_kw)

    if cfg.mode == "contract":
        c = effective_contract_kw(cfg)
        band = cfg.over_contract_band * c
        over = max(0.0, peak_kw - band)
        charge = c * p_kw + cfg.over_band_price_multiplier * p_kw * over
        return BasicCharge(cfg.mode, peak_kw, c, band, over, charge)

    raise ValueError(f"未知的基本电价计费方式 mode={cfg.mode!r}，可选：{MODES}")


def with_mode(cfg: DemandTariffConfig, mode: str) -> DemandTariffConfig:
    """换一种计费方式（配置是 frozen 的，只能复制着改）。"""
    if mode not in MODES:
        raise ValueError(f"未知计费方式 {mode!r}，可选：{MODES}")
    return DemandTariffConfig(**{**cfg.__dict__, "mode": mode})


def with_contract_kw(cfg: DemandTariffConfig, contract_kw: float) -> DemandTariffConfig:
    return DemandTariffConfig(**{**cfg.__dict__, "contract_demand_kw": float(contract_kw)})


def grid_import_kw(load_kw: Sequence[float], charge_kw: Sequence[float],
                   discharge_kw: Sequence[float]) -> np.ndarray:
    """电网侧取电功率 = 负荷 + 充电（从电网买） − 放电（顶上去）。"""
    return (np.asarray(load_kw, dtype=float) + np.asarray(charge_kw, dtype=float)
            - np.asarray(discharge_kw, dtype=float))


def month_peak_kw(day_grids: Sequence[Sequence[float]]) -> float:
    """整月最大需量：所有日、所有 15 分钟点里的最大电网取电功率。"""
    return float(max(float(np.asarray(g, dtype=float).max()) for g in day_grids))


def demand_saving_yuan(no_storage_peak_kw: float, with_storage_peak_kw: float,
                       *, cfg: DemandTariffConfig) -> float:
    """需量节省 = 装储能前的月基本电费 − 装储能后的。

    合同制下这一步经常是 0，而且**这不是 bug**：未超过核定值 105% 时按核定值收取，
    把月峰从 2399 削到 2300 一分钱不省。任何"储能需量管理收益"的说法都得先过这一关。
    """
    return (basic_charge(no_storage_peak_kw, cfg=cfg).charge_yuan
            - basic_charge(with_storage_peak_kw, cfg=cfg).charge_yuan)


def daily_provision_yuan(monthly_charge_yuan: float, days_in_billing_month: int) -> float:
    """月账单按日均摊，给日报表用一个"今天的电费里含多少基本电费"的口径。

    ⚠️ 这只是**分摊**，不是"这一天的需量电费"。真正要日度计费时必须记住：
    日均日峰值 ≤ 月峰值，所以"按当天峰值 × 单价 ÷ 月天数"会**低估**月账单，
    负荷越不均匀低估越多。月账单一律以 `basic_charge(month_peak, ...)` 为准。
    """
    if days_in_billing_month <= 0:
        raise ValueError("计费月天数必须为正")
    return monthly_charge_yuan / float(days_in_billing_month)


# ---------------------------------------------------------------------- #
#  月度复盘 CLI
# ---------------------------------------------------------------------- #
def aggregate(rows: Sequence[Dict]) -> Dict:
    """把逐日记录合成月度结论：月峰 = 各日峰值的最大值，"日峰值平均" = 各日峰值均值。

    单独拆出来是因为整月复盘要在**分片进程**里跑：本机 HiGHS 连续求解到第 13 个模型时
    抛过 `MemoryError: bad allocation`，一个进程吃下 30 天不可靠，所以逐日结果落盘、
    最后由这里合并（`--emit` 负责落盘与续跑）。
    """
    if not rows:
        raise ValueError("没有逐日记录可汇总")

    def _stat(key):
        vals = [float(r[key]) for r in rows if key in r]
        if not vals:
            return None
        return {"peak": max(vals), "mean_daily_peak": float(np.mean(vals)), "days": len(vals)}

    stats = {"no_storage": _stat("load_peak_kw"), "baseline": _stat("base_peak_kw"),
             "optimized": _stat("opt_peak_kw")}
    out = {"days_in_month": len(rows), "per_day": list(rows), "peaks": {},
           "daily_peak_mean": {}}
    for key, s in stats.items():
        if s:
            out["peaks"][key] = s["peak"]
            out["daily_peak_mean"][key] = s["mean_daily_peak"]
    return out


def read_emitted(path: str) -> List[Dict]:
    """读回已落盘的逐日记录（同一日期后写的覆盖先写的，方便重跑某一天）。"""
    import json
    import os
    by_date: Dict[str, Dict] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date)]


def _row_is_reusable(row: Dict, *, run_milp: bool,
                     demand_cap_kw: Optional[float]) -> bool:
    """已落盘的这一行能不能顶替本轮要算的那一天。

    两个条件缺一不可，都是实测踩出来的：
    1. 要跑 MILP 时，行里必须真的有优化列——否则 `--no-milp` 先写一遍，
       后面真正的求解就会把这些日期全跳过，白跑一圈还拿不到数；
    2. 上限值必须一致——开环行与闭环行说的是两件不同的事，混用等于伪造对照。
    """
    if run_milp and not all(k in row for k in ("opt_peak_kw", "opt_net_yuan")):
        return False
    got = row.get("opt_cap_kw")
    if demand_cap_kw is None:
        return got is None
    return got is not None and abs(float(got) - float(demand_cap_kw)) < 1e-9


def run_month(df, dates: List[str], *, cfg=CONFIG, run_milp: bool = True,
              verbose: bool = True, emit: Optional[str] = None,
              demand_cap_kw: Optional[float] = None) -> Dict:
    """对内置数据集逐日跑「无储能 / 基准策略 / 价格优化 MILP」三种情形，跟踪整月月峰。

    注意这里"价格优化"用的就是仓库现在这套**只看电价**的目标函数——本轮不往目标里
    加需量项，所以得到的调度并不是"为削峰而优化"的解，而是"顺手会不会削到峰"的实测答案。

    `emit` 给出路径时按日追加写 JSONL，并**跳过文件里已有的日期**（续跑）。逐日只留
    标量峰值、不把 96 点曲线攒在内存里，也是为分片跑准备的。
    """
    import gc
    import json

    from src.agents.storage_optimization_agent import StorageOptimizationAgent
    from src.data.data_loader import day_price_temp, get_day_data

    done: Dict[str, Dict] = {}
    if emit:
        done = {r["date"]: r for r in read_emitted(emit)
                if _row_is_reusable(r, run_milp=run_milp, demand_cap_kw=demand_cap_kw)}
        dates = [d for d in dates if d not in done]
        if verbose and done:
            print(f"续跑：可复用 {len(done)} 天，本轮还剩 {len(dates)} 天", flush=True)
        fh = open(emit, "a", encoding="utf-8", newline="\n")
    else:
        fh = None

    agent = StorageOptimizationAgent(cfg)
    rows: List[Dict] = []

    try:
        for i, date in enumerate(dates, start=1):
            load = get_day_data(df, date)["load_kw"].to_numpy(dtype=float)
            pt = day_price_temp(df, date)
            if pt is None:
                raise RuntimeError(f"{date} 不足 96 点，无法参与月度复盘")
            price, ambient = pt

            base = agent.baseline_strategy(price, ambient)
            grid_base = grid_import_kw(load, base.charge_power_kw, base.discharge_power_kw)
            row = {"date": date, "load_peak_kw": float(load.max()),
                   "base_peak_kw": float(grid_base.max())}

            if run_milp:
                # 开环那一臂刻意不传负荷：模型里没有别的地方用得到它，
                # 传了会被 optimize() 的成对校验挡下来（上限与负荷必须同生同灭）。
                cap_kw = ({} if demand_cap_kw is None else
                          {"load_profile_kw": load, "demand_cap_kw": demand_cap_kw})
                opt = agent.optimize(price, ambient_temp_profile=ambient,
                                     include_thermal_constraint=True,
                                     include_degradation_cost=True, dr_signal=None,
                                     **cap_kw)
                grid_opt = grid_import_kw(load, opt.charge_power_kw, opt.discharge_power_kw)
                row.update(opt_peak_kw=float(grid_opt.max()),
                           opt_arbitrage_yuan=opt.arbitrage_revenue_yuan,
                           opt_net_yuan=opt.net_revenue_yuan)
                if demand_cap_kw is not None:
                    row["opt_cap_kw"] = float(demand_cap_kw)
                del opt, grid_opt

            rows.append(row)
            if fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            if verbose:
                print(f"[{i}/{len(dates)}] {date} 负荷峰 {row['load_peak_kw']:.0f} kW"
                      f" · 基准 {row['base_peak_kw']:.0f} kW"
                      + (f" · 优化 {row['opt_peak_kw']:.0f} kW" if run_milp else ""),
                      flush=True)
            del base, grid_base, row
            gc.collect()
    finally:
        if fh:
            fh.close()

    return aggregate(read_emitted(emit) if emit else rows)


def to_markdown(res: Dict, *, cfgs: Dict[str, DemandTariffConfig],
                contract_sweep: Sequence[float]) -> str:
    peaks = res["peaks"]
    means = res["daily_peak_mean"]
    lines = [
        f"# 基本电费月度复盘（{res['days_in_month']} 天）",
        "",
        "## 1. 需量峰值",
        "",
        "| 情形 | 月最大需量 kW | 日峰值平均 kW | 峰值被摊掉的量 |",
        "|---|---|---|---|",
    ]
    for key, label in (("no_storage", "无储能"), ("baseline", "基准策略（谷充尖放）"),
                       ("optimized", "价格优化 MILP")):
        if key not in peaks:
            continue
        lines.append(f"| {label} | {peaks[key]:.0f} | {means[key]:.0f} "
                     f"| {peaks[key] - means[key]:+.0f} |")
    lines += ["", "> 「日峰值平均」比「月最大需量」低的这一截就是按日均摊会低估月账单的量。", ""]

    lines += ["", "## 2. 三种计费方式下的月基本电费（元/月）", "",
              "| 计费方式 | 单价 | 无储能 | 基准策略 | 价格优化 | 优化省多少 |",
              "|---|---|---|---|---|---|"]
    # 单价从配置来，不在此处再抄一份字面量（tests/test_demand_charge.py 有断言守着）
    d_p = cfgs["contract"].demand_price_yuan_per_kw_month
    c_p = cfgs["contract"].capacity_price_yuan_per_kva_month
    unit = {"capacity": f"{c_p} 元/千伏安·月", "contract": f"{d_p} 元/千瓦·月",
            "actual": f"{d_p} 元/千瓦·月", "none": "—"}
    has_opt = "optimized" in peaks
    for mode, scfg in cfgs.items():
        row = [f"| {mode} | {unit[mode]}"]
        for key in ("no_storage", "baseline", "optimized"):
            if key in peaks:
                row.append(f"{basic_charge(peaks[key], cfg=scfg).charge_yuan:,.0f}")
            else:
                row.append("—")
        saving = ((basic_charge(peaks["no_storage"], cfg=scfg).charge_yuan
                   - basic_charge(peaks["optimized"], cfg=scfg).charge_yuan)
                  if has_opt else None)
        row.append(f"{saving:+,.0f}" if saving is not None else "—")
        lines.append(" | ".join(row) + " |")

    if not has_opt:
        return "\n".join(lines) + "\n"

    lines += ["", "## 3. 合同核定值扫描：优化调度到底能不能省基本电费", "",
              "| 合同核定 kW | 105% 阈值 kW | 优化后月峰 | 超出 kW | 无储能账单 | 优化后账单 | 省 |",
              "|---|---|---|---|---|---|---|"]
    for c in contract_sweep:
        scfg = with_contract_kw(with_mode(cfgs["contract"], "contract"), c)
        r_no = basic_charge(peaks["no_storage"], cfg=scfg)
        r_opt = basic_charge(peaks["optimized"], cfg=scfg)
        lines.append(
            f"| {c:,.0f} | {r_no.band_kw:,.0f} | {peaks['optimized']:,.0f} | "
            f"{r_opt.over_band_kw:,.0f} | {r_no.charge_yuan:,.0f} | "
            f"{r_opt.charge_yuan:,.0f} | "
            f"{r_no.charge_yuan - r_opt.charge_yuan:+,.0f} |")

    band_of = basic_charge(peaks["no_storage"],
                           cfg=with_mode(cfgs["contract"], "contract")).band_kw
    best_c = peaks["no_storage"] / cfgs["contract"].over_contract_band
    lines += ["",
              f"本工况无储能月峰 {peaks['no_storage']:.0f} kW、优化后 {peaks['optimized']:.0f} kW，"
              f"对默认核定值 {effective_contract_kw(cfgs['contract']):,.0f} kW 的 105% 阈值 "
              f"{band_of:,.0f} kW —— 在不在阈值以内，直接决定上面「省」这一列是不是全 0。",
              "",
              f"顺带一个真实业务里能直接做的事：合同核定值申报到 **月峰 ÷ "
              f"{cfgs['contract'].over_contract_band:.0%} = "
              f"{best_c:,.0f} kW** 时基本电费最低（再报低就开始触发超额加倍，"
              f"再报高就是白交）。这条与储能无关，但储能把月峰压低之后，最优申报值会跟着降，"
              "两边要一起看。"]
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="基本电费（容/需量电价）月度复盘")
    ap.add_argument("--month", default="2024-07", help="内置数据里的月份，默认 2024-07")
    ap.add_argument("--no-milp", action="store_true", help="只跑无储能与基准，不求 MILP（秒级）")
    ap.add_argument("--emit", metavar="JSONL",
                    help="逐日结果追加写到这里；文件里已有的日期自动跳过（分片/续跑用）")
    ap.add_argument("--report-only", action="store_true",
                    help="不求解，只用 --emit 指到的 JSONL 出报告")
    ap.add_argument("--contract-sweep", default="2000,2100,2200,2300,2400,2500",
                    help="合同核定值扫描点，逗号分隔（kW）")
    ap.add_argument("--demand-cap", type=float, default=None, metavar="kW",
                    help="启用闭环：给 MILP 加每日电网取电上限（不填=开环，只事后计费）")
    ap.add_argument("--out", help="把 markdown 报告写到这个文件")
    args = ap.parse_args(list(argv) if argv is not None else None)

    from src.data.data_loader import load_load_data

    if args.report_only:
        if not args.emit:
            print("--report-only 需要配合 --emit 指定 JSONL", file=sys.stderr)
            return 2
        res = aggregate(read_emitted(args.emit))
    else:
        df = load_load_data()
        dates = sorted({t.date().isoformat() for t in df["timestamp"]
                        if t.strftime("%Y-%m") == args.month})
        if not dates:
            print(f"内置数据里没有 {args.month} 这个月", file=sys.stderr)
            return 2
        res = run_month(df, dates, cfg=CONFIG, run_milp=not args.no_milp, emit=args.emit,
                        demand_cap_kw=args.demand_cap)

    cfgs = {m: with_mode(CONFIG.demand, m) for m in ("capacity", "contract", "actual", "none")}
    sweep = [float(x) for x in args.contract_sweep.split(",") if x.strip()]
    md = to_markdown(res, cfgs=cfgs, contract_sweep=sweep)
    print(md)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline=chr(10)) as fh:
            fh.write(md)
        print(f"已保存：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

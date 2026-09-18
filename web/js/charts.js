/* ================= ECharts 图表层（双主题） ================= */
function chartPalette() {
  const dark = State.theme === "dark";
  return dark ? {
    charge: "#4cc3ff", discharge: "#ff8a3d", price: "#ffb25e", soc: "#ff8a3d",
    temp: "#ff5c5c", ambient: "#4cc3ff", load: "#4cc3ff", loadAct: "#8a8798",
    text: "#d5d2dc", title: "#f4f2ee", grid: "rgba(255,255,255,0.08)", axis: "rgba(255,255,255,0.18)",
    seq: ["#ff8a3d", "#4cc3ff", "#ffd666", "#9b8aff", "#35d0c5"],
  } : {
    charge: "#165dff", discharge: "#ff7d00", price: "#ff7d00", soc: "#ff7d00",
    temp: "#f53f3f", ambient: "#165dff", load: "#165dff", loadAct: "#86909c",
    text: "#4e5969", title: "#1d2129", grid: "rgba(29,33,41,0.08)", axis: "rgba(29,33,41,0.2)",
    seq: ["#165dff", "#14c9c9", "#f7ba1e", "#722ed1", "#ff7d00"],
  };
}

const AX_TEXT = () => ({ color: chartPalette().text, fontSize: 10.5 });

function chartOption() {
  const p = chartPalette();
  return {
    animationDuration: 600,
    textStyle: { color: p.text, fontFamily: "-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif", fontSize: 11 },
    tooltip: {
      trigger: "axis",
      backgroundColor: State.theme === "dark" ? "rgba(23,23,31,0.96)" : "#ffffff",
      borderColor: State.theme === "dark" ? "rgba(255,255,255,0.14)" : "#e5e6eb",
      textStyle: { color: p.title, fontSize: 12 },
      axisPointer: { lineStyle: { color: p.axis } },
    },
  };
}

function mountChart(el, option) {
  const inst = echarts.init(el, null, { renderer: "canvas" });
  inst.setOption(option);
  State.charts.push(inst);
  return inst;
}

function reflowCharts() {
  requestAnimationFrame(() => State.charts.forEach((c) => c.resize()));
}

/* 删除 priceBands 死代码——该函数硬编码 7 段电价时段且从未被任何图表
   调用，与 engineCfg().price_periods（/api/bootstrap 下发的单一事实来源）冲突；
   若未来需要电价底纹，应从 engineCfg().price_periods 动态生成 markArea。 */

function drMarkAreas(windows) {
  return windows.map((w) => [{ xAxis: w.start }, { xAxis: w.end },
    { itemStyle: { color: "rgba(255,138,61,0.08)" } }]);
}

/* ---------- 通用坐标轴 ---------- */
function axis(opts = {}) {
  return Object.assign({
    axisLine: { lineStyle: { color: chartPalette().axis } },
    axisLabel: AX_TEXT(),
    splitLine: { lineStyle: { color: chartPalette().grid } },
  }, opts);
}

/* ========== 图1：总览 24h 三联图 ========== */
function chartSched24(el, d) {
  const p = chartPalette();
  const hours = d.series.hours;
  const opt = {
    ...chartOption(),
    height: 560,
    grid: [{ left: 52, right: 20, top: 42, height: "26%" },
           { left: 52, right: 20, top: "44%", height: "22%" },
           { left: 52, right: 20, top: "74%", height: "20%" }],
    legend: { top: 0, textStyle: { color: p.text, fontSize: 10.5 }, itemWidth: 14 },
    xAxis: [0, 1, 2].map((i) => axis({ type: "category", data: hours, gridIndex: i,
      axisLabel: { ...AX_TEXT(), interval: 11 }, axisTick: { show: false } })),
    yAxis: [
      axis({ type: "value", gridIndex: 0, name: "功率(kW)", nameTextStyle: { color: p.title, fontSize: 10 } }),
      axis({ type: "value", gridIndex: 1, name: "SOC(%)", nameTextStyle: { color: p.title, fontSize: 10 }, min: 0, max: 100 }),
      axis({ type: "value", gridIndex: 2, name: "负荷(kW)", nameTextStyle: { color: p.title, fontSize: 10 } }),
    ],
    series: [
      { name: "充电", type: "bar", xAxisIndex: 0, yAxisIndex: 0, data: d.series.charge_power,
        barWidth: "55%", itemStyle: { color: p.charge, opacity: 0.88 }, tooltip: { valueFormatter: (v) => v + " kW" } },
      { name: "放电", type: "bar", xAxisIndex: 0, yAxisIndex: 0, data: d.series.discharge_power,
        barWidth: "55%", itemStyle: { color: p.discharge, opacity: 0.88 }, tooltip: { valueFormatter: (v) => v + " kW" } },
      { name: "电价", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: d.series.price,
        lineStyle: { color: p.price, width: 1.6, type: "dotted" }, symbol: "none", tooltip: { valueFormatter: (v) => "¥" + v + "/kWh" } },
      { name: "SOC", type: "line", xAxisIndex: 1, yAxisIndex: 1, data: d.series.soc, symbol: "none",
        lineStyle: { color: p.soc, width: 2.4 },
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: State.theme === "dark" ? "rgba(255,138,61,0.22)" : "rgba(255,125,0,0.16)" },
          { offset: 1, color: "rgba(255,138,61,0.02)" }]) },
        markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed", color: p.temp },
          label: { color: p.text, fontSize: 9.5 },
          data: [{ yAxis: d.soc_range[1], label: { formatter: "SOC上限" } }, { yAxis: d.soc_range[0], label: { formatter: "SOC下限" } }] } },
      { name: "预测负荷", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: d.series.load_forecast,
        symbol: "none", lineStyle: { color: p.load, width: 1.6 } },
    ],
    tooltipShow: true,
  };
  if (d.series.load_actual) {
    opt.series.push({ name: "实际负荷", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: d.series.load_actual,
      symbol: "none", lineStyle: { color: p.loadAct, width: 1, type: "dashed" } });
  }
  if (d.dr_windows.length) {
    opt.series[0].markArea = { silent: true, data: drMarkAreas(d.dr_windows) };
  }
  mountChart(el, opt);
}

/* ========== 图2：瀑布图（支持文字标注） ========== */
function chartWaterfall(el, wf, withText = false) {
  const p = chartPalette();
  // 逐列计算 [垫层高度, 柱体高度, 颜色]
  const colors = [p.discharge, p.charge, p.temp, p.discharge];
  const labels = [`+${wf.y[0].toFixed(0)}`, `+${wf.y[1].toFixed(0)}`, `-${Math.abs(wf.y[2]).toFixed(0)}`, `=${wf.total.toFixed(0)}`];
  const values = [...wf.y, wf.total];
  let acc = 0;
  const cols = values.map((v, i) => {
    let bottom, height;
    if (i < 3) { bottom = Math.min(acc, acc + v); height = Math.abs(v); acc += v; }
    else { bottom = Math.min(0, v); height = Math.abs(v); }
    return { bottom, height, color: colors[i] };
  });
  mountChart(el, {
    ...chartOption(),
    grid: { left: 56, right: 20, top: withText ? 34 : 30, bottom: 26 },
    xAxis: axis({ type: "category", data: [...wf.x], axisTick: { show: false } }),
    yAxis: axis({ type: "value", name: withText ? "元" : "", nameTextStyle: { color: p.title, fontSize: 10 } }),
    tooltip: { ...chartOption().tooltip, formatter: (params) => `${wf.x[params.dataIndex]}<br/><b>${labels[params.dataIndex]} 元</b>` },
    series: [
      { type: "bar", stack: "w", barWidth: "44%", silent: true, tooltip: { show: false },
        data: cols.map((c) => ({ value: c.bottom, itemStyle: { color: "transparent" } })) },
      { type: "bar", stack: "w", barWidth: "44%",
        data: cols.map((c) => ({ value: c.height, itemStyle: { color: c.color, borderRadius: [3, 3, 0, 0] } })),
        label: withText ? { show: true, position: "top", color: p.title, fontSize: 10,
          formatter: (pr) => labels[pr.dataIndex] } : undefined },
    ],
  });
}

/* ========== 图4：调度页 充放电 + SOC 双行 ========== */
function chartPowerSoc(el, d) {
  const p = chartPalette();
  const hours = d.series.hours;
  mountChart(el, {
    ...chartOption(),
    grid: [{ left: 56, right: 22, top: 40, height: "38%" }, { left: 56, right: 22, top: "60%", height: "30%" }],
    legend: { top: 0, textStyle: { color: p.text, fontSize: 10.5 } },
    xAxis: [
      axis({ type: "category", data: hours, gridIndex: 0, axisLabel: { ...AX_TEXT(), interval: 11 } }),
      axis({ type: "category", data: hours, gridIndex: 1, name: "时间(h)", nameTextStyle: { color: p.title, fontSize: 10 }, axisLabel: { ...AX_TEXT(), interval: 11 } }),
    ],
    yAxis: [
      axis({ type: "value", gridIndex: 0, name: "功率(kW)", nameTextStyle: { color: p.title, fontSize: 10 } }),
      axis({ type: "value", gridIndex: 1, name: "SOC(%)", min: 0, max: 100, nameTextStyle: { color: p.title, fontSize: 10 } }),
    ],
    series: [
      { name: "充电", type: "bar", xAxisIndex: 0, yAxisIndex: 0, data: d.series.charge_power, barWidth: "55%", itemStyle: { color: p.charge, opacity: 0.88 } },
      { name: "放电", type: "bar", xAxisIndex: 0, yAxisIndex: 0, data: d.series.discharge_power, barWidth: "55%", itemStyle: { color: p.discharge, opacity: 0.88 } },
      { name: "电价", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: d.series.price, symbol: "none", lineStyle: { color: p.price, width: 1.6, type: "dotted" } },
      { name: "SOC", type: "line", xAxisIndex: 1, yAxisIndex: 1, data: d.series.soc, symbol: "none",
        lineStyle: { color: p.soc, width: 2.4 },
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: State.theme === "dark" ? "rgba(255,138,61,0.2)" : "rgba(255,125,0,0.15)" },
          { offset: 1, color: "rgba(255,138,61,0.02)" }]) },
        markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed", color: p.temp },
          label: { color: p.text, fontSize: 9.5 },
          data: [{ yAxis: d.soc_range[1] }, { yAxis: d.soc_range[0] }] } },
    ],
  });
}

/* ========== 图5：温度趋势（电池+环境+警戒带+电价底纹） ========== */
function chartTemp(el, d, scope) {
  const p = chartPalette();
  const hours = d.eng.series.hours;
  const mask = hours.map((h) => h >= scope[0] && h <= scope[1]);
  const pick = (arr) => arr.filter((_, i) => mask[i]);
  mountChart(el, {
    ...chartOption(),
    grid: { left: 50, right: 20, top: 40, bottom: 30 },
    legend: { top: 0, textStyle: { color: p.text, fontSize: 10.5 } },
    xAxis: axis({ type: "category", data: hours, min: "dataMin", max: "dataMax",
      axisLabel: { ...AX_TEXT(), interval: 11 }, name: "时间(h)", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    yAxis: axis({ type: "value", name: "温度(℃)", min: 20, max: Math.max(70, d.eng.report.max_battery_temp_c + 5),
      nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    series: [
      { name: "电池温度", type: "line", data: pick(d.eng.series.battery_temp), symbol: "none",
        lineStyle: { color: p.temp, width: 2.4 },
        markArea: { silent: true, data: [
          [{ yAxis: 45, itemStyle: { color: "rgba(255,176,32,0.08)" } }, { yAxis: 55 }],
          [{ yAxis: 55, itemStyle: { color: "rgba(255,92,92,0.10)" } }, { yAxis: 80 }],
        ].map(([a, b]) => [a, b]) },
        markLine: { silent: true, symbol: "none",
          data: [{ yAxis: 45, lineStyle: { color: p.price, type: "dashed", width: 1 } },
                 { yAxis: 55, lineStyle: { color: p.temp, type: "dashed", width: 1 } }] } },
      { name: "环境温度", type: "line", data: pick(d.eng.series.ambient_temp), symbol: "none",
        lineStyle: { color: p.ambient, width: 1.2, type: "dashed" } },
    ],
  });
}

/* ========== 图6：负荷预测 vs 实际（可选修正区域标注） ========== */
function chartLoad(el, d, withPhysZone = false, seriesKey = "eng") {
  const p = chartPalette();
  const s = d[seriesKey] ? d[seriesKey].series : d.series;
  const series = [
    { name: "预测负荷", type: "line", data: s.load_forecast, symbol: "none",
      lineStyle: { color: p.load, width: 2 },
      areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
        { offset: 0, color: State.theme === "dark" ? "rgba(76,195,255,0.14)" : "rgba(22,93,255,0.10)" },
        { offset: 1, color: "rgba(76,195,255,0.01)" }]) } },
  ];
  if (s.load_actual && !(seriesKey === "eng" && d.has_actual === false && s.load_actual.every((v, i) => v === s.load_forecast[i]))) {
    series.push({ name: "实际负荷", type: "line", data: s.load_actual, symbol: "none",
      lineStyle: { color: p.loadAct, width: 1.1, type: "dashed" } });
  }
  if (withPhysZone) {
    series[0].markArea = { silent: true, data: [
      [{ xAxis: 10, itemStyle: { color: "rgba(255,138,61,0.07)" }, label: { show: true, position: "insideTop", color: p.discharge, fontSize: 9.5, formatter: "传热学物理修正区域" } }, { xAxis: 16 }],
    ] };
  }
  mountChart(el, {
    ...chartOption(),
    grid: { left: 52, right: 18, top: 38, bottom: 30 },
    legend: { top: 0, textStyle: { color: p.text, fontSize: 10.5 } },
    xAxis: axis({ type: "category", data: s.hours, axisLabel: { ...AX_TEXT(), interval: 11 }, name: "时间(h)", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    yAxis: axis({ type: "value", name: "负荷(kW)", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    series,
  });
}

/* ========== 图7：功率（负=充）+ SOC 双轴 ========== */
function chartPowerDual(el, d) {
  const p = chartPalette();
  const s = d.eng.series;
  mountChart(el, {
    ...chartOption(),
    grid: { left: 54, right: 54, top: 40, bottom: 30 },
    legend: { top: 0, textStyle: { color: p.text, fontSize: 10.5 } },
    xAxis: axis({ type: "category", data: s.hours, axisLabel: { ...AX_TEXT(), interval: 11 }, name: "时间(h)", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    yAxis: [
      axis({ type: "value", name: "功率(kW) 负=充 正=放", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
      axis({ type: "value", name: "SOC(%)", min: 0, max: 100, splitLine: { show: false }, nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    ],
    series: [
      { name: "充电", type: "bar", data: s.charge_power.map((v) => -v), stack: "p", barWidth: "55%", itemStyle: { color: p.charge, opacity: 0.85 } },
      { name: "放电", type: "bar", data: s.discharge_power, stack: "p", barWidth: "55%", itemStyle: { color: p.discharge, opacity: 0.85 } },
      { name: "SOC", type: "line", yAxisIndex: 1, data: s.soc, symbol: "none", lineStyle: { color: p.soc, width: 2.4 } },
    ],
  });
}

/* ========== 图8：水平条形（特征重要性 / 衰减系数） ========== */
function chartHBar(el, names, values, opts = {}) {
  const p = chartPalette();
  // 修复：散装 rgba 分支收敛到 chartPalette() 单一来源，用 opacity 做层次衰减
  const colors = opts.colors || names.map((_, i) => ({ color: p.discharge, opacity: 0.95 - i * 0.12 }));
  mountChart(el, {
    ...chartOption(),
    grid: { left: 96, right: 44, top: opts.title ? 30 : 10, bottom: 22 },
    xAxis: axis({ type: "value", show: opts.showX !== false, max: opts.max,
      splitLine: { show: false }, axisLabel: { show: !!opts.showX } }),
    yAxis: axis({ type: "category", data: names, inverse: true, splitLine: { show: false },
      axisLine: { show: false }, axisTick: { show: false }, axisLabel: { ...AX_TEXT(), fontSize: 10.5 } }),
    series: [{
      type: "bar", data: values.map((v, i) => ({ value: v,
        itemStyle: typeof colors[i] === "string" ? { color: colors[i] } : { color: colors[i].color, opacity: colors[i].opacity } })),
      barWidth: "56%", itemStyle: { borderRadius: [0, 4, 4, 0] },
      label: { show: true, position: "right", color: p.text, fontSize: 10,
        formatter: (pr) => opts.fmt ? opts.fmt(pr.value) : pr.value },
    }],
    tooltip: { show: false },
    title: opts.title ? { text: opts.title, left: 0, top: 0, textStyle: { color: p.title, fontSize: 12, fontWeight: 600 } } : undefined,
  });
}

/* ========== 图9：直方图（误差分布） ========== */
function chartHist(el, hist) {
  const p = chartPalette();
  mountChart(el, {
    ...chartOption(),
    grid: { left: 44, right: 14, top: 30, bottom: 28 },
    xAxis: axis({ type: "category", data: hist.map((h) => h.mid),
      axisLabel: { ...AX_TEXT(), interval: 3 }, name: "误差(%)", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    yAxis: axis({ type: "value", name: "频次", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    series: [{ type: "bar", data: hist.map((h) => h.count), barWidth: "92%", itemStyle: { color: p.charge, opacity: 0.85 } }],
    tooltip: { ...chartOption().tooltip, formatter: (pr) => `误差 ${hist[pr.dataIndex].mid}%<br/>频次 ${hist[pr.dataIndex].count}` },
  });
}

/* ========== 图10：工作日/周末形态 ========== */
function chartProfile(el, prof) {
  const p = chartPalette();
  mountChart(el, {
    ...chartOption(),
    grid: { left: 52, right: 16, top: 38, bottom: 28 },
    legend: { top: 0, textStyle: { color: p.text, fontSize: 10.5 } },
    xAxis: axis({ type: "category", data: prof.hours, axisLabel: { ...AX_TEXT(), interval: 11 }, name: "时间(h)", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    yAxis: axis({ type: "value", name: "负荷(kW)", nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    series: [
      { name: "工作日", type: "line", data: prof.weekday, symbol: "none", connectNulls: true, lineStyle: { color: p.discharge, width: 1.6 } },
      { name: "周末", type: "line", data: prof.weekend, symbol: "none", connectNulls: true, lineStyle: { color: p.charge, width: 1.6 } },
    ],
  });
}

/* ========== 图11：DR 时间轴（甘特） ========== */
function chartGantt(el, details) {
  const p = chartPalette();
  const items = details.map((d, i) => {
    const [sh, sm] = d.time.split("-")[0].split(":").map(Number);
    const [eh, em] = d.time.split("-")[1].split(":").map(Number);
    return { name: `DR#${i + 1} ${d.time}`, start: sh + sm / 60, end: eh + em / 60,
      accepted: d.accepted, target: d.target, actual: d.actual,
      rate: d.target > 0 ? Math.min(100, d.actual / d.target * 100) : 0 };
  });
  const xs = items.flatMap((i) => [i.start, i.end]);
  // 布局①：按事件数量自适应容器高度——事件少时不再大片留白，事件多时等比增高
  const n = items.length;
  el.style.height = Math.max(190, n * 74 + 62) + "px";
  mountChart(el, {
    ...chartOption(),
    // 布局②：containLabel 为富文本标签留空间；right:56 给 x 轴名「时间(h)」让位（原 30 被裁）
    grid: { left: 16, right: 56, top: 14, bottom: 30, containLabel: true },
    // 布局③：改 item 触发让每条 bar 的自定义 formatter 生效
    tooltip: { ...chartOption().tooltip, trigger: "item" },
    xAxis: axis({ type: "value", min: Math.max(0, Math.floor(Math.min(...xs)) - 1),
      max: Math.min(24, Math.ceil(Math.max(...xs)) + 1), name: "时间(h)", nameGap: 10,
      nameTextStyle: { color: p.title, fontSize: 9.5 } }),
    yAxis: axis({ type: "category", data: items.map((i) => `{n|${i.name}}\n{s|目标${i.target}kW · 完成率${i.rate.toFixed(0)}%}`),
      inverse: true, axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { formatter: (v) => v, rich: {
        n: { color: p.title, fontSize: 10.5, fontWeight: 600, lineHeight: 16 },
        s: { color: p.text, fontSize: 9, lineHeight: 13 } } }, splitLine: { show: false } }),
    series: items.map((it, i) => ({
      type: "bar", barWidth: 22, barMaxWidth: 26,
      // 布局④：行序修正——inverse:true 时 index 0 在顶部，y 直接用 i，
      // 此前 length-1-i 配 inverse 导致事件条与标签行错位
      data: [[it.start, i]],
      itemStyle: { color: it.accepted ? p.discharge : p.temp, opacity: 0.88, borderRadius: 6 },
      tooltip: { formatter: () => `<b>${it.name}</b><br/>${it.accepted ? "✓ 接受" : "✗ 拒绝"} · 目标 ${it.target}kW · 实际 ${it.actual}kW<br/>完成率 ${it.rate.toFixed(0)}%` },
    })),
  });
}

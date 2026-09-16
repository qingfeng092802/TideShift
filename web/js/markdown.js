/* ================= 轻量 Markdown 渲染（对话气泡专用） =================
   P1 修复：对话回答此前以纯文本渲染（textContent / esc），LLM 输出的
   `## 标题`、`| 表格 |`、`**加粗**` 全部原样露出，可读性差。

   设计约束：
   1. 不引第三方库——CSP 的 script-src 只放行本地脚本，且 echarts 也是
      web/vendor/ 本地分发，本项目不依赖 CDN。
   2. 安全：先对整段文本做 HTML 转义，再按【白名单标签】生成结构；
      任何内容都无法注入标签或属性。链接仅放行 http/https。
   3. 容错：流式输出时语法可能不完整（未闭合代码块、半截表格），
      一律降级为普通文本，不抛异常、不吞内容。

   支持子集：标题(#~####)、无序/有序列表(含一层嵌套)、表格、代码块、
            行内代码、加粗、斜体、引用、分隔线、链接。
   ====================================================================== */
function renderMarkdown(src) {
  const text = String(src == null ? "" : src).replace(/\r\n?/g, "\n");
  const lines = esc(text).split("\n");
  const out = [];
  let i = 0;

  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  const isBlockStart = (t) => /^(#{1,6}\s|```|&gt;\s?|([-*+]|\d+\.)\s|\|)/.test(t)
    || /^(-{3,}|\*{3,}|_{3,})$/.test(t);

  while (i < lines.length) {
    const line = lines[i].trim();

    if (!line) { i++; continue; }

    /* ---------- 代码块 ---------- */
    const fence = line.match(/^```(\w*)/);
    if (fence) {
      i++;
      const buf = [];
      while (i < lines.length && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i++; }
      const closed = i < lines.length;
      if (closed) i++;
      const lang = fence[1] ? ` data-lang="${esc(fence[1])}"` : "";
      out.push(`<pre class="md-code${closed ? "" : " md-code-open"}"${lang}>`
        + `<code>${buf.join("\n")}</code></pre>`);
      continue;
    }

    /* ---------- 分隔线 ---------- */
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) { out.push('<hr class="md-hr">'); i++; continue; }

    /* ---------- 标题 ---------- */
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lv = Math.min(h[1].length, 4);
      out.push(`<h${lv} class="md-h${lv}">${inline(h[2].replace(/\s*#+\s*$/, ""))}</h${lv}>`);
      i++;
      continue;
    }

    /* ---------- 表格（表头行 + 分隔行） ---------- */
    if (/^\|.*\|$/.test(line) && i + 1 < lines.length
        && /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\|.*\|$/.test(lines[i].trim())) { rows.push(cells(lines[i])); i++; }
      out.push('<div class="md-table-wrap"><table class="md-table"><thead><tr>'
        + head.map((c) => `<th>${inline(c)}</th>`).join("")
        + "</tr></thead><tbody>"
        + rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("")
        + "</tbody></table></div>");
      continue;
    }

    /* ---------- 引用（> 已被转义为 &gt;） ---------- */
    if (/^&gt;\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i].trim())) {
        buf.push(lines[i].trim().replace(/^&gt;\s?/, ""));
        i++;
      }
      out.push(`<blockquote class="md-quote">${inline(buf.join(" "))}</blockquote>`);
      continue;
    }

    /* ---------- 列表（支持一层嵌套 + 续行合并） ---------- */
    if (/^([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\d+\.\s/.test(line);
      const items = [];
      while (i < lines.length) {
        const rawLine = lines[i];
        const t = rawLine.trim();
        const m = t.match(/^([-*+]|\d+\.)\s+(.*)$/);
        const indented = /^\s{2,}/.test(rawLine);
        if (m) {
          if (indented && items.length) items[items.length - 1].sub.push(m[2]);
          else items.push({ text: m[2], sub: [] });
          i++;
        } else if (t && items.length && !isBlockStart(t)) {
          const last = items[items.length - 1];
          if (indented && last.sub.length) last.sub[last.sub.length - 1] += " " + t;
          else last.text += " " + t;
          i++;
        } else break;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag} class="md-list">` + items.map((it) =>
        `<li>${inline(it.text)}` + (it.sub.length
          ? `<${tag} class="md-list">` + it.sub.map((s) => `<li>${inline(s)}</li>`).join("") + `</${tag}>`
          : "") + "</li>").join("") + `</${tag}>`);
      continue;
    }

    /* ---------- 段落（连续行合并，行内换行保留为 <br>） ---------- */
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i].trim())) {
      buf.push(lines[i].trim());
      i++;
    }
    out.push(`<p class="md-p">${inline(buf.join("<br>"))}</p>`);
  }

  return '<div class="md">' + out.join("") + "</div>";
}

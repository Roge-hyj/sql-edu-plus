/**
 * 轻量 Markdown → HTML 转换器，供 rich-text 渲染 AI 反馈与对话气泡。
 *
 * 支持：``` 代码块、# ~ ### 标题、**粗体**、_斜体_、`行内代码`、
 * "- " 列表行、换行。先统一 escapeHtml 防注入，代码块用占位符保护。
 */

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function renderMarkdown(md: string | null | undefined): string {
  if (!md) return "";
  const source = escapeHtml(String(md));

  // 1. 抽出围栏代码块，避免内部再走行内规则
  const codeBlocks: string[] = [];
  const withPlaceholders = source.replace(
    /```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g,
    (_m, _lang, body) => {
      codeBlocks.push(
        `<pre class="md-pre"><code>${String(body).replace(/\n$/, "")}</code></pre>`,
      );
      return `\u0000CODE${codeBlocks.length - 1}\u0000`;
    },
  );

  const renderInline = (text: string): string =>
    text
      .replace(/`([^`\n]+)`/g, '<code class="md-code">$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>");

  // 2. 逐行处理标题 / 列表 / 普通段落
  const lines = withPlaceholders.split("\n");
  const out: string[] = [];
  let inList = false;

  const closeList = () => {
    if (inList) {
      out.push("</ul>");
      inList = false;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (/^\u0000CODE\d+\u0000$/.test(line.trim())) {
      closeList();
      const idx = Number(line.trim().match(/\d+/)![0]);
      out.push(codeBlocks[idx]);
      continue;
    }
    if (!line.trim()) {
      closeList();
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + 2, 5);
      out.push(`<div class="md-h${level}">${renderInline(heading[2])}</div>`);
      continue;
    }
    const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      if (!inList) {
        out.push('<ul class="md-ul">');
        inList = true;
      }
      out.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    closeList();
    out.push(`<div class="md-p">${renderInline(line)}</div>`);
  }
  closeList();
  return out.join("");
}

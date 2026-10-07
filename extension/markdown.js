// Markdown -> HTML for analyses, written here rather than pulled from a CDN
// (MV3 forbids remote code) and deliberately small: headings, paragraphs,
// lists, tables, quotes, code, links, emphasis.
//
// Safety: an answer can quote the transcript, which is untrusted text. Every
// piece of source text is escaped before any tag is added, the only tags
// emitted are the ones below, and links must be http(s).
//
// Three things on top of plain markdown, so an answer reads like the design:
//   - timestamps (05:08, 1:02:30, 09:45–12:00) become links that seek the video
//   - verdict words in a table cell (**Correct**, **Misleading**) become tags
//   - a list under a "worth watching" heading renders as segment cards

const Markdown = (() => {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const TS = String.raw`(?:\d{1,2}:)?[0-5]?\d:[0-5]\d`;
  // Not preceded by a digit or colon; not followed by more digits. A colon
  // right after is fine — "**03:00–03:38:** the Shpilkin estimate".
  const TS_RE = new RegExp(String.raw`(?<![\d:])(${TS})(?:(\s*(?:–|—|-|→|to)\s*)(${TS}))?(?!\d|:\d)`, "g");

  function seconds(ts) {
    return ts.split(":").reduce((acc, n) => acc * 60 + Number(n), 0);
  }

  function length(sec) {
    if (sec < 60) return `${sec} s`;
    const m = Math.floor(sec / 60), s = sec % 60;
    return s ? `${m} min ${s} s` : `${m} min`;
  }

  function timestamps(s) {
    return s.replace(TS_RE, (all, a, sep, b) => {
      const start = seconds(a);
      if (!b) return `<a class="ts" href="#" data-t="${start}">${a}</a>`;
      const end = seconds(b);
      const len = end > start ? ` data-len="${length(end - start)}"` : "";
      return `<a class="ts range" href="#" data-t="${start}"${len}>${a}${esc(sep)}${b}</a>`;
    });
  }

  // Inline spans. Code and links are lifted out first into placeholders so
  // nothing later (emphasis, timestamps) reaches inside them.
  function inline(src, { links = true, stamps = true } = {}) {
    const slots = [];
    const keep = (html) => `\u0000${slots.push(html) - 1}\u0000`;
    let s = src.replace(/`([^`]+)`/g, (_, c) => keep(`<code>${esc(c)}</code>`));
    if (links) {
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)\)/g,
        (_, text, url) => keep(`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${inline(text, { links: false })}</a>`));
      s = s.replace(/https?:\/\/[^\s<>()\u0000]+[^\s<>().,;:!?'"»”\u0000]/g,
        (url) => keep(`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`));
    }
    s = esc(s);
    if (stamps) s = timestamps(s);   // not in headings: "(12:49)" there is a runtime
    s = s.replace(/\*\*(?=\S)(.+?)(?<=\S)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*(?=\S)(.+?)(?<=\S)\*(?![*\w])/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^\w])_(?=\S)(.+?)(?<=\S)_(?!\w)/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[Number(i)]);
  }

  const VERDICT = [
    ["ok", /^(mostly |largely |broadly )?(correct|accurate|true|confirmed|right)\b/i],
    ["off", /^(misleading|wrong|false|incorrect|outdated|exaggerated|overstated|partly|partially|mis\w*|inaccurate)\b/i],
    ["unknown", /^(unverified|unverifiable|could not|couldn't|not found|unclear|contested|disputed)\b/i],
  ];

  function tagVerdict(cellHtml) {
    return cellHtml.replace(/^<strong>([^<]*)<\/strong>/, (all, text) => {
      for (const [cls, re] of VERDICT) {
        if (re.test(text.trim())) return `<strong class="tag ${cls}">${text}</strong>`;
      }
      return all;
    }).replace(/^([A-Za-z][A-Za-z ']{2,24})$/, (all, text) => {
      for (const [cls, re] of VERDICT) {
        if (re.test(text.trim())) return `<span class="tag ${cls}">${text}</span>`;
      }
      return all;
    });
  }

  // --- blocks --------------------------------------------------------------

  const RE = {
    fence: /^\s*(```|~~~)/,
    heading: /^(#{1,6})\s+(.*?)\s*#*\s*$/,
    hr: /^\s*([-*_])(\s*\1){2,}\s*$/,
    quote: /^\s*>\s?/,
    item: /^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$/,
    tableSep: /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/,
  };

  const indentOf = (l) => l.match(/^\s*/)[0].replace(/\t/g, "    ").length;
  const isBlank = (l) => !l.trim();
  const startsBlock = (l, next) =>
    RE.fence.test(l) || RE.heading.test(l) || RE.hr.test(l) || RE.quote.test(l) || RE.item.test(l)
    || (l.includes("|") && next !== undefined && RE.tableSep.test(next));

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  }

  function table(lines, i) {
    const head = splitRow(lines[i]);
    const align = splitRow(lines[i + 1]).map((c) =>
      c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "");
    i += 2;
    const body = [];
    while (i < lines.length && lines[i].includes("|") && !isBlank(lines[i])) body.push(splitRow(lines[i++]));
    const cell = (tag, c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ""}>${tag === "td" ? tagVerdict(inline(c)) : inline(c)}</${tag}>`;
    const html = `<div class="table-wrap"><table><thead><tr>${head.map((c, k) => cell("th", c, k)).join("")}</tr></thead>`
      + `<tbody>${body.map((r) => `<tr>${head.map((_, k) => cell("td", r[k] ?? "", k)).join("")}</tr>`).join("")}</tbody></table></div>`;
    return [html, i];
  }

  function list(lines, i, ctx) {
    const first = lines[i].match(RE.item);
    const base = indentOf(lines[i]);
    const ordered = /\d/.test(first[2]);
    const sameKind = (m) => m && /\d/.test(m[2]) === ordered;
    const items = [];
    while (i < lines.length) {
      const l = lines[i];
      if (isBlank(l)) {
        // A blank line ends the list unless more of it follows.
        let j = i + 1;
        while (j < lines.length && isBlank(lines[j])) j++;
        if (j < lines.length && (indentOf(lines[j]) > base
            || (sameKind(lines[j].match(RE.item)) && indentOf(lines[j]) === base))) { i = j; continue; }
        break;
      }
      const m = l.match(RE.item);
      const ind = indentOf(l);
      if (m && ind === base && !sameKind(m)) break;   // bullets -> numbers: a new list
      if (m && ind === base) {
        items.push({ text: m[3], sub: [] });
      } else if (ind > base && items.length) {
        items.at(-1).sub.push(l);
      } else if (!m && items.length && !startsBlock(l, lines[i + 1])) {
        items.at(-1).text += " " + l.trim();   // lazy continuation
      } else {
        break;
      }
      i++;
    }
    const tag = ordered ? "ol" : "ul";
    const cls = ctx.segments ? ` class="segments"` : "";
    const lis = items.map((it) => {
      let sub = "";
      if (it.sub.length) {
        const cut = Math.min(...it.sub.filter((l) => !isBlank(l)).map(indentOf));
        sub = blocks(it.sub.map((l) => l.slice(Math.min(cut, indentOf(l)))), { segments: false });
      }
      return `<li>${inline(it.text)}${sub}</li>`;
    }).join("");
    return [`<${tag}${cls}>${lis}</${tag}>`, i];
  }

  function blocks(lines, ctx = { segments: false }) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (isBlank(l)) { i++; continue; }

      if (RE.fence.test(l)) {
        const fence = l.trim().slice(0, 3);
        const code = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) code.push(lines[i++]);
        i++;
        out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
        continue;
      }
      let m = l.match(RE.heading);
      if (m) {
        const level = Math.min(m[1].length + 1, 5);
        ctx.segments = /worth (watching|your time|the time)|stretches|skim|segments/i.test(m[2]);
        out.push(`<h${level}>${inline(m[2], { stamps: false })}</h${level}>`);
        i++;
        continue;
      }
      if (RE.hr.test(l)) { out.push("<hr>"); i++; continue; }
      if (l.includes("|") && i + 1 < lines.length && RE.tableSep.test(lines[i + 1])) {
        const [html, next] = table(lines, i);
        out.push(html);
        i = next;
        continue;
      }
      if (RE.quote.test(l)) {
        const q = [];
        while (i < lines.length && !isBlank(lines[i]) && RE.quote.test(lines[i])) q.push(lines[i++].replace(RE.quote, ""));
        out.push(`<blockquote>${blocks(q, { segments: false })}</blockquote>`);
        continue;
      }
      if (RE.item.test(l)) {
        const [html, next] = list(lines, i, ctx);
        out.push(html);
        i = next;
        continue;
      }
      const para = [];
      while (i < lines.length && !isBlank(lines[i]) && (para.length === 0 || !startsBlock(lines[i], lines[i + 1]))) {
        para.push(lines[i++].trim());
      }
      // A bold-only line such as "**Worth watching**" also opens a segment section.
      if (para.length === 1 && /^\*\*[^*]+\*\*:?$/.test(para[0])) {
        ctx.segments = /worth (watching|your time|the time)|stretches|skim|segments/i.test(para[0]);
      }
      out.push(`<p>${inline(para.join(" "))}</p>`);
    }
    return out.join("");
  }

  return {
    render(text) {
      return blocks((text || "").replace(/\r\n?/g, "\n").split("\n"));
    },
  };
})();

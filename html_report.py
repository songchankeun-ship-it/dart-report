"""
마크다운 리포트 → 브라우저용 HTML 변환 + reports/index.html 목록 페이지.
외부 패키지 없이 동작 (간이 마크다운 렌더러: 제목, 표, 목록, 인용, <details>, 굵게, 링크).
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;--card:#f9fafb;--accent:#2563eb;
--pos:#15803d;--neg:#b91c1c;--neu:#6b7280;--hold:#b45309}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e5e7eb;--muted:#9ca3af;--line:#262a33;--card:#161a22;--accent:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:18px;margin:34px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:26px 0 8px}
table{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0 14px;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}th{background:var(--card)}
td:has(> .n){text-align:right}
blockquote{margin:10px 0;padding:8px 14px;border-left:4px solid var(--accent);background:var(--card);color:var(--muted);font-size:14px}
blockquote.quote{border-color:var(--muted);font-style:italic}
details{margin:8px 0}summary{cursor:pointer;color:var(--accent)}
ul{padding-left:22px}li{margin:2px 0}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
hr{border:0;border-top:1px dashed var(--line);margin:24px 0}
.meta{color:var(--muted);font-size:13px}
.sig-pos{color:var(--pos);font-weight:600}.sig-neg{color:var(--neg);font-weight:600}.sig-neu{color:var(--neu);font-weight:600}.sig-hold{color:var(--hold);font-weight:600}
.warn{background:#fef3c7;color:#92400e;padding:8px 12px;border-radius:6px;margin:8px 0;font-size:14px}
@media(prefers-color-scheme:dark){.warn{background:#3b2f0b;color:#fcd34d}}
.card{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0;background:var(--card)}
.idx a.item{display:flex;justify-content:space-between;gap:12px;padding:10px 12px;border-bottom:1px solid var(--line)}
.idx a.item:hover{background:var(--card);text-decoration:none}
.topnav{font-size:13px;margin-bottom:16px}
"""

_SIG = {"긍정적": "sig-pos", "부정적": "sig-neg", "중립": "sig-neu", "판단보류": "sig-hold"}


def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"(?<![\"'>=])(https?://[^\s<)]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w])_(.+?)_(?![\w])", r"<i>\1</i>", s)
    for k, cls in _SIG.items():
        s = s.replace(k, f'<span class="{cls}">{k}</span>')
    return s


def md_to_html(md: str, title: str, nav: str = "") -> str:
    out, i, lines = [], 0, md.splitlines()
    in_list = False
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            hdr = [c.strip() for c in ln.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(h)}</th>" for h in hdr) + "</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue
        if ln.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline(ln[2:])}</li>")
            i += 1
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        if ln.startswith("# "):
            out.append(f"<h1>{_inline(ln[2:])}</h1>")
        elif ln.startswith("## "):
            out.append(f"<h2>{_inline(ln[3:])}</h2>")
        elif ln.startswith("### "):
            out.append(f"<h3>{_inline(ln[4:])}</h3>")
        elif ln.startswith("> "):
            out.append(f"<blockquote>{_inline(ln[2:])}</blockquote>")
        elif ln.startswith("<details") or ln.startswith("</details") or ln.startswith("<summary"):
            out.append(ln if "<summary" not in ln else ln)
        elif ln.strip() == "---":
            out.append("<hr>")
        elif ln.startswith("⚠"):
            out.append(f'<div class="warn">{_inline(ln)}</div>')
        elif ln.startswith("기간:") or ln.startswith("생성 ") or ln.startswith("데이터:"):
            out.append(f'<p class="meta">{_inline(ln)}</p>')
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    if in_list:
        out.append("</ul>")
    body = "\n".join(out)
    return (f"<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>"
            f"{('<div class=topnav>' + nav + '</div>') if nav else ''}{body}</main></body></html>")


def write_html(md_path: Path, nav: str = "") -> Path:
    md = md_path.read_text("utf-8")
    title = next((l[2:] for l in md.splitlines() if l.startswith("# ")), md_path.stem)
    out = md_path.with_suffix(".html")
    out.write_text(md_to_html(md, title, nav), "utf-8")
    return out


def build_index(root: Path) -> Path:
    """root 아래 모든 .html 리포트 목록 (최신순)."""
    items = []
    for p in root.rglob("*.html"):
        if p.name == "index.html":
            continue
        md = p.with_suffix(".md")
        title = p.stem
        if md.exists():
            title = next((l[2:] for l in md.read_text("utf-8").splitlines() if l.startswith("# ")), p.stem)
        items.append((p.stat().st_mtime, p.relative_to(root).as_posix(), title))
    items.sort(reverse=True)
    rows = "".join(f'<a class="item" href="{html.escape(rel)}"><span>{html.escape(t)}</span>'
                   f'<span class="meta">{datetime.fromtimestamp(m):%Y-%m-%d %H:%M}</span></a>' for m, rel, t in items)
    page = (f"<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>공시 리포트 목록</title><style>{CSS}</style></head><body><main><h1>공시 리포트</h1>"
            f"<p class=meta>{len(items)}개 · 최신순</p><div class=idx>{rows or '<p>아직 리포트가 없습니다.</p>'}</div></main></body></html>")
    out = root / "index.html"
    out.write_text(page, "utf-8")
    return out

"""reports/ 를 정적 사이트 폴더로 복사하고 상단 진입 페이지(index.html)를 만든다.  python site_build.py reports site"""
import html, shutil, sys
from datetime import datetime
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.md", "*.json", ".last_run"))

daily = sorted((dst / "daily").glob("일일공시_*.html"), reverse=True)
boards = sorted((dst / "stocks").glob("*.html")) if (dst / "stocks").exists() else []
boards = [b for b in boards if b.name != "index.html"]

def severity(md_path: Path) -> tuple:
    try:
        t = md_path.read_text("utf-8")
    except FileNotFoundError:
        return (0, 0, 0)
    return (t.count("\n| 🔴"), t.count("\n| 🟠"), t.count("\n| 🟡"))


def flags_of(md_path: Path) -> str:
    try:
        lines = md_path.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return ""
    out = []
    for ln in lines:
        if ln.startswith("| 🔴") or ln.startswith("| 🟠") or ln.startswith("| 🟡"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            out.append(f"{cells[0]} {cells[1].strip('*')}")
    return " · ".join(out[:4]) + (f" · 외 {len(out) - 4}" if len(out) > 4 else "") if out else "신호 없음"

css = """body{margin:0;background:#fff;color:#1a1a1a;font:15px/1.6 -apple-system,'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;padding-bottom:6px;border-bottom:1px solid #e5e7eb}
.meta{color:#6b7280;font-size:13px}a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}
.item{display:block;padding:10px 12px;border-bottom:1px solid #eee}.item:hover{background:#f9fafb;text-decoration:none}
.sub{color:#6b7280;font-size:13px;margin-top:2px}.warn{font-size:13px;color:#6b7280;margin-top:24px}
@media(prefers-color-scheme:dark){body{background:#0f1115;color:#e5e7eb}.item{border-color:#262a33}.item:hover{background:#161a22}h2{border-color:#262a33}}"""
L = [f"<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>공시 리포트</title><style>{css}</style></head><body><main>",
     f"<h1>공시 리포트</h1><p class=meta>마지막 갱신 {datetime.now():%Y-%m-%d %H:%M} KST · DART 공시 기반 사실 자료, 투자 권유 아님</p>"]
L.append("<h2>일일 리포트</h2>")
for p in daily[:30]:
    stem = p.stem.replace("일일공시_", "")
    a, b = stem.split("_") if "_" in stem else (stem, stem)
    L.append(f"<a class=item href='daily/{html.escape(p.name)}'>{a[:4]}-{a[4:6]}-{a[6:]} ~ {b[:4]}-{b[4:6]}-{b[6:]}</a>")
if not daily:
    L.append("<p class=meta>아직 없음</p>")
L.append("<h2>종목별 리스크 보드 <span class=meta>(신호 많은 순)</span></h2>")
boards.sort(key=lambda p: severity(Path(src) / "stocks" / (p.stem + ".md")), reverse=True)
for p in boards:
    md = Path(src) / "stocks" / (p.stem + ".md")
    L.append(f"<a class=item href='stocks/{html.escape(p.name)}'>{html.escape(p.stem)}<div class=sub>{html.escape(flags_of(md))}</div></a>")
L.append("<p class=warn>주의 신호는 정해진 표시 기준(리포트에 기준값 명시)에 따른 사실 표시이며 매수·매도 의견이 아닙니다. 모든 수치는 DART 원문 링크로 확인할 수 있습니다.</p>")
L.append("</main></body></html>")
(dst / "index.html").write_text("\n".join(L), "utf-8")
(dst / ".nojekyll").write_text("")
print(f"site: {len(daily)} daily, {len(boards)} boards → {dst}")

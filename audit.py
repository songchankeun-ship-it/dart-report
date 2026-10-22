"""
정확도 전수 대조 도구 (테스트 1)

시장 전체 최근 공시 중 전용 파서 6종 대상 유형을 모아서 파싱하고,
  파싱값  |  원문에서 그 값이 나온 줄(근거)  |  확인란
을 나란히 놓은 대조표를 만든다. 사람이 원문 링크 열어서 하나씩 체크하는 데 쓰는 용도.

  python audit.py --days 5 --per-type 15          # 유형별 최대 15건, 최근 5일
  python audit.py --days 3 --types 메자닌발행,유상증자
  python audit.py --corps watchlist.txt --days 30  # 관심종목만

출력 audit/<날짜>/
  audit.html   대조표 (브라우저)
  audit.csv    같은 내용 (엑셀)
  fill_rate.md 유형별·필드별 채움률 — 비는 필드 = 정규식 보강 대상
  raw/<rcept_no>.txt  원문 텍스트 (대조용)
"""
from __future__ import annotations

import argparse
import csv
import html
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("dart.env")   # 윈도우 탐색기에서 보이는 이름
except ImportError:
    pass

from dart_client import DartClient, DartError, Disclosure, days_ago
from analyzer import classify
from parsers import PARSERS, parse_structured
from pipeline import load_watchlist

TARGET = list(PARSERS.keys())
# 시장 전체 조회 시 유형별 DART 공시유형 코드 (B=주요사항보고, I=거래소공시, F=외부감사관련, D=지분공시)
PBLNTF_TY = ["B", "I", "F", "D"]


# ------------------------------------------------------------------ 근거 찾기
def _fmt_candidates(v) -> list[str]:
    if isinstance(v, bool) or v is None:
        return []
    if isinstance(v, int):
        return [f"{v:,}", str(v)]
    if isinstance(v, float):
        s = f"{v:g}"
        return [s, s.rstrip("0").rstrip("."), f"{v:.2f}", f"{v:.1f}"]
    if isinstance(v, str):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", v)
        if m:
            y, mo, d = m.groups()
            return [v, f"{y}년 {int(mo):02d}월 {int(d):02d}일", f"{y}년 {int(mo)}월 {int(d)}일", f"{y}.{mo}.{d}", f"{y}/{mo}/{d}"]
        return [v]
    return []


def evidence(text: str, value) -> str:
    """원문에서 값이 등장하는 첫 줄 (앞뒤 조금)을 반환. 못 찾으면 ''."""
    lines = text.splitlines()
    for cand in _fmt_candidates(value):
        if not cand:
            continue
        for i, ln in enumerate(lines):
            if cand in ln:
                ctx = " ⏎ ".join(x.strip() for x in lines[max(0, i - 1): i + 1] if x.strip())
                return ctx[:220]
    return ""


DERIVED = {"환산_전환주식수", "리픽싱_하한비율", "리픽싱후_최대주식수", "리픽싱후_최대비율", "사모여부",
           "신주_합계", "희석률", "조달예정액", "조달액_기준", "제3자배정", "계약기간_일수", "연환산_계약금액", "조건부",
           "지분율_변화", "주당_인수가", "주식수_합계", "주당_예정단가", "비적정", "실행시_지분율_하락", "담보비율", "양식"}


# ------------------------------------------------------------------ 수집
def collect(client: DartClient, bgn: str, end: str, per_type: int, types: list[str],
            corps: list[str] | None) -> list[Disclosure]:
    found: dict[str, list[Disclosure]] = defaultdict(list)
    seen = set()

    def take(d: Disclosure):
        c = classify(d)
        if c.category in types and d.rcept_no not in seen and len(found[c.category]) < per_type:
            seen.add(d.rcept_no)
            found[c.category].append(d)

    if corps:
        for q in corps:
            try:
                code = client.find_corp(q)["corp_code"]
                for d in client.list_disclosures(code, bgn, end):
                    take(d)
            except DartError as e:
                print(f"[{q}] {e}", file=sys.stderr)
    else:
        for ty in PBLNTF_TY:
            try:
                for d in client.list_disclosures("", bgn, end, pblntf_ty=ty):
                    take(d)
            except DartError as e:
                print(f"[유형 {ty}] {e}", file=sys.stderr)
            if all(len(found[t]) >= per_type for t in types):
                break
    out = [d for t in types for d in found[t]]
    print("수집:", {t: len(found[t]) for t in types})
    return out


# ------------------------------------------------------------------ 메인
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--per-type", type=int, default=15)
    ap.add_argument("--types", default=",".join(TARGET))
    ap.add_argument("--corps", help="관심종목 파일 (없으면 시장 전체)")
    ap.add_argument("--out", default="audit")
    args = ap.parse_args(argv)

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    bgn, end = days_ago(args.days), f"{date.today():%Y%m%d}"
    try:
        client = DartClient()
    except DartError as e:
        print("오류:", e, file=sys.stderr)
        return 1
    corps = load_watchlist(args.corps) if args.corps else None
    discs = collect(client, bgn, end, args.per_type, types, corps)
    if not discs:
        print("대상 공시가 없습니다. --days 를 늘려보세요.")
        return 0

    out_dir = Path(args.out) / f"{date.today():%Y%m%d}"
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    rows = []                                   # (회사, 공시, 유형, url, rcept, 필드, 값, 파생?, 근거)
    fill: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    warnings = []

    for d in discs:
        cat = classify(d).category
        try:
            text = client.fetch_document_text(d.rcept_no, max_chars=200_000)
        except DartError as e:
            warnings.append((d, f"원문 실패: {e}"))
            continue
        (out_dir / "raw" / f"{d.rcept_no}.txt").write_text(text, "utf-8")
        s = parse_structured(cat, text) or {}
        if s.get("_warning") or s.get("_error"):
            warnings.append((d, s.get("_warning") or s.get("_error")))
        for k, v in s.items():
            if k.startswith("_"):
                continue
            derived = k in DERIVED
            filled = v not in (None, {}, [], "")
            fill[cat][k][0] += filled
            fill[cat][k][1] += 1
            ev = "" if derived or isinstance(v, (dict, list)) else evidence(text, v)
            vs = v if not isinstance(v, (dict, list)) else str(v)[:200]
            rows.append((d.corp_name, d.report_nm, cat, d.url, d.rcept_no, k, vs, derived, ev,
                         "" if (filled and (ev or derived or isinstance(v, (dict, list)))) else "⚠근거못찾음" if filled else "빈값"))

    # ---- CSV
    with open(out_dir / "audit.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["회사", "공시", "유형", "원문URL", "접수번호", "필드", "파싱값", "파생치", "원문근거", "자동플래그", "사람확인(O/X)", "메모"])
        for r in rows:
            w.writerow([*r[:7], "파생" if r[7] else "", r[8], r[9], "", ""])

    # ---- 채움률
    L = [f"# 필드 채움률  ({bgn}~{end}, {len(discs)}건)", ""]
    for cat in types:
        if cat not in fill:
            continue
        L.append(f"## {cat}")
        L.append("| 필드 | 채움 | 전체 | 비율 |")
        L.append("|---|---:|---:|---:|")
        for k, (a, b) in sorted(fill[cat].items(), key=lambda kv: kv[1][0] / max(kv[1][1], 1)):
            L.append(f"| {k}{' (파생)' if k in DERIVED else ''} | {a} | {b} | {a / b * 100:.0f}% |")
        L.append("")
    if warnings:
        L.append("## 경고")
        L += [f"- [{d.corp_name}] {d.report_nm} — {msg} — {d.url}" for d, msg in warnings]
    (out_dir / "fill_rate.md").write_text("\n".join(L), "utf-8")

    # ---- HTML 대조표
    def esc(x): return html.escape(str(x), quote=False)
    H = ["<!doctype html><html lang=ko><head><meta charset=utf-8><title>파서 대조표</title><style>",
         "body{font:14px/1.5 -apple-system,'Malgun Gothic','Noto Sans KR',sans-serif;margin:20px;color:#1a1a1a}",
         "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:5px 8px;vertical-align:top;text-align:left}",
         "th{background:#f3f4f6;position:sticky;top:0}tr.head td{background:#eef2ff;font-weight:600}",
         ".d{color:#6b7280}.w{background:#fef3c7}.e{background:#fee2e2}.ev{color:#374151;font-size:13px}",
         "input[type=checkbox]{transform:scale(1.3)}</style></head><body>",
         f"<h1>파서 대조표 {bgn}~{end}</h1><p>{len(discs)}건 · 각 값 옆의 '원문근거'가 실제로 그 값을 말하는지, 원문 링크를 열어 확인하고 체크. "
         f"노란색 = 값은 있는데 원문에서 근거를 자동으로 못 찾음(사람이 꼭 확인), 빨간색 = 빈값.</p>",
         "<table><tr><th>필드</th><th>파싱값</th><th>원문근거</th><th>플래그</th><th>확인</th></tr>"]
    cur = None
    for r in rows:
        key = r[4]
        if key != cur:
            cur = key
            H.append(f"<tr class=head><td colspan=5>{esc(r[0])} — {esc(r[1])} <span class=d>[{esc(r[2])}]</span> "
                     f"&nbsp; <a href='{r[3]}' target=_blank>원문</a></td></tr>")
        cls = "w" if r[9].startswith("⚠") else "e" if r[9] == "빈값" else ""
        H.append(f"<tr class='{cls}'><td>{esc(r[5])}{' <span class=d>(파생)</span>' if r[7] else ''}</td><td>{esc(r[6]) if r[6] is not None else '<span class=d>—</span>'}</td>"
                 f"<td class=ev>{esc(r[8])}</td><td>{esc(r[9])}</td><td><input type=checkbox></td></tr>")
    H.append("</table></body></html>")
    (out_dir / "audit.html").write_text("\n".join(H), "utf-8")

    n_warn = sum(1 for r in rows if r[9].startswith("⚠"))
    n_empty = sum(1 for r in rows if r[9] == "빈값")
    print(f"\n저장: {out_dir}/audit.html  (필드 {len(rows)}개 · 근거못찾음 {n_warn} · 빈값 {n_empty} · 양식경고 {len(warnings)}건)")
    print(f"채움률: {out_dir}/fill_rate.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
분기 실적 시계열 (DART 정기보고서 재무제표 API 기반)

  - 분기 3개월 손익: 1분기·3분기 보고서는 thstrm_amount 가 해당 3개월, 반기 보고서는 thstrm_amount 가 2분기 3개월(누적은 thstrm_add_amount).
    사업보고서는 연간만 있으므로 4분기 = 연간 − 3분기 누적.
  - 전년 동기(frmtrm_q_amount / frmtrm_add_amount) 가 같이 오므로 YoY 는 API 값으로, QoQ 는 우리가 모은 시계열로.
  - 연결(CFS) 우선, 없으면 개별(OFS).

  python quarterly.py 삼성전자
"""
from __future__ import annotations

import json
import sys
from datetime import date

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("dart.env")
except ImportError:
    pass

from dart_client import DartClient, DartError

Q_CODES = [("11013", "1Q"), ("11012", "2Q"), ("11014", "3Q"), ("11011", "4Q")]
PL = {"매출액": ("매출액", "수익(매출액)", "영업수익", "매출", "매출및지분법손익", "수익"),
      "영업이익": ("영업이익", "영업이익(손실)", "영업손익"),
      "순이익": ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익", "분기순이익(손실)", "반기순이익(손실)",
              "분기순손실", "반기순손실", "당기순손실", "연결당기순이익", "연결분기순이익", "연결반기순이익",
              "지배기업의소유주에게귀속되는당기순이익(손실)", "지배기업소유주지분순이익", "지배기업의소유주에게귀속되는분기순이익(손실)",
              "지배기업의소유주에게귀속되는반기순이익(손실)", "법인세비용차감전순이익(손실)")}
BS = {"자산총계": ("자산총계",), "부채총계": ("부채총계",), "자본총계": ("자본총계",)}


def _num(v):
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _pick(rows: list[dict], aliases: tuple, sj: tuple = ("손익계산서", "포괄손익계산서")) -> dict | None:
    for r in rows:
        if r.get("sj_nm") in sj and r.get("account_nm", "").replace(" ", "") in aliases:
            return r
    return None


def fetch_year(client: DartClient, corp_code: str, year: int) -> dict:
    """한 해의 분기별 {Q: {매출액, 영업이익, 순이익, 매출액_전년, ...}}. 없는 분기는 생략."""
    out = {}
    annual_cum = {}
    for code, q in Q_CODES:
        rows = []
        for fs in ("CFS", "OFS"):
            try:
                rows = client._get_json("fnlttSinglAcntAll.json", corp_code=corp_code, bsns_year=str(year),
                                        reprt_code=code, fs_div=fs).get("list", []) or []
            except DartError:
                rows = []
            if rows:
                break
        if not rows:
            continue
        rec = {"_fs": fs}
        for key, al in PL.items():
            r = _pick(rows, al)
            if not r:
                continue
            if q == "4Q":
                rec[key + "_연간"] = _num(r.get("thstrm_amount"))
                rec[key + "_연간_전년"] = _num(r.get("frmtrm_amount"))
            else:
                rec[key] = _num(r.get("thstrm_amount"))
                rec[key + "_누적"] = _num(r.get("thstrm_add_amount")) or (rec[key] if q == "1Q" else None)
                rec[key + "_전년"] = _num(r.get("frmtrm_q_amount")) or (_num(r.get("frmtrm_amount")) if q == "1Q" else None)
                rec[key + "_누적_전년"] = _num(r.get("frmtrm_add_amount")) or (rec[key + "_전년"] if q == "1Q" else None)
        for key, al in BS.items():
            r = _pick(rows, al, ("재무상태표",))
            if r:
                rec[key] = _num(r.get("thstrm_amount"))
        out[q] = rec
    # 4분기 3개월 = 연간 − 3분기 누적
    if "4Q" in out and "3Q" in out:
        for key in PL:
            a, c3 = out["4Q"].get(key + "_연간"), out["3Q"].get(key + "_누적")
            a_p, c3_p = out["4Q"].get(key + "_연간_전년"), out["3Q"].get(key + "_누적_전년")
            if a is not None and c3 is not None:
                q4 = a - c3
                # 연간 < 3분기 누적(이익 계정에서 부호 반전 등) 이면 재작성·정정 가능성 → 값은 두되 의심 표시
                suspicious = (key != "매출액" and c3 < 0 and q4 > 0 and abs(q4) > abs(c3) * 0.5) or \
                             (key == "매출액" and (q4 < 0 or (c3 > 0 and q4 > c3 * 3)))
                out["4Q"][key] = q4
                if suspicious:
                    out["4Q"][key + "_의심"] = True
            if a_p is not None and c3_p is not None:
                out["4Q"][key + "_전년"] = a_p - c3_p
    return out


def series(client: DartClient, corp_code: str, years: int = 2) -> list[dict]:
    """최근 N년 분기 시계열 [{기간:'2026 2Q', 매출액, 영업이익, 순이익, YoY, QoQ}] 오래된 것부터."""
    y = date.today().year
    rows = []
    for yr in range(y - years, y + 1):
        data = fetch_year(client, corp_code, yr)
        for code, q in Q_CODES:
            if q in data and any(data[q].get(k) is not None for k in PL):
                rows.append({"기간": f"{yr} {q}", "년": yr, "분기": q, **data[q]})
    for i, r in enumerate(rows):
        for key in PL:
            cur, prev_y = r.get(key), r.get(key + "_전년")
            r[key + "_YoY"] = _pct(cur, prev_y)
            r[key + "_QoQ"] = _pct(cur, rows[i - 1].get(key)) if i > 0 and not rows[i - 1].get(key + "_의심") else None
        s, op = r.get("매출액"), r.get("영업이익")
        r["영업이익률"] = round(op / s * 100, 1) if s and op is not None else None
        a, l, e = r.get("자산총계"), r.get("부채총계"), r.get("자본총계")
        r["부채비율"] = round(l / e * 100, 1) if l is not None and e and e > 0 else None
    return rows[-(years * 4 + 1):]


def _pct(cur, base):
    if cur is None or base in (None, 0):
        return None
    if base < 0:
        return None            # 적자 기준 증감률은 의미 없음 → 표시 안 함
    return round((cur - base) / abs(base) * 100, 1)


def signals(rows: list[dict]) -> list[str]:
    """사실 기반 플래그 문장 (판단 아님)."""
    out = []
    if len(rows) < 2:
        return out
    last = rows[-1]
    ops = [r.get("영업이익") for r in rows[-4:] if r.get("영업이익") is not None and not r.get("영업이익_의심")]
    if len(ops) >= 4 and all(v < 0 for v in ops):
        out.append(f"최근 4개 분기 연속 영업적자 (~{last['기간']})")
    elif ops and ops[-1] is not None and ops[-1] < 0:
        out.append(f"{last['기간']} 영업적자 {ops[-1] / 1e8:,.0f}억")
    sales = [r.get("매출액") for r in rows if r.get("매출액") is not None]
    yoy = [r.get("매출액_YoY") for r in rows[-2:] if r.get("매출액_YoY") is not None]
    if len(yoy) == 2 and all(v <= -20 for v in yoy):
        out.append(f"매출 전년 동기 대비 2분기 연속 20% 이상 감소 ({yoy[0]}%, {yoy[1]}%)")
    e = last.get("자본총계")
    if e is not None and e <= 0:
        out.append(f"{last['기간']} 완전자본잠식 (자본총계 {e / 1e8:,.0f}억)")
    elif last.get("부채비율") and last["부채비율"] >= 300:
        out.append(f"{last['기간']} 부채비율 {last['부채비율']}%")
    return out


def table_md(rows: list[dict]) -> list[str]:
    if not rows:
        return ["_분기 데이터 없음._"]
    L = ["| 분기 | 매출액 | YoY | QoQ | 영업이익 | YoY | 영업이익률 | 순이익 | 부채비율 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        sus = "⚠" if any(r.get(k + "_의심") for k in PL) else ""
        L.append(f"| {r['기간']}{sus} | {_w(r.get('매출액'))} | {_p(r.get('매출액_YoY'))} | {_p(r.get('매출액_QoQ'))} | {_w(r.get('영업이익'))} | "
                 f"{_p(r.get('영업이익_YoY'))} | {_p(r.get('영업이익률'), signed=False)} | {_w(r.get('순이익'))} | {_p(r.get('부채비율'), signed=False)} |")
    fs = rows[-1].get("_fs", "")
    note = " · ⚠ = 연간 값이 3분기 누적과 크게 어긋나 4분기 도출값 신뢰 불가(재무제표 재작성·정정 가능성, 원문 확인)" if any(any(r.get(k + "_의심") for k in PL) for r in rows) else ""
    L.append(f"_단위 억원 · {'연결' if fs == 'CFS' else '개별' if fs == 'OFS' else ''} 기준 · 4분기 = 연간 − 3분기 누적 · 출처 DART 정기보고서{note}_")
    return L


def _w(n):
    return f"{n / 1e8:,.0f}" if isinstance(n, int) else "-"


def _p(v, suffix="%", signed=True):
    if v is None:
        return "-"
    return f"{v:+.1f}{suffix}" if signed else f"{v:.1f}{suffix}"


def main(argv=None):
    client = DartClient()
    corp = client.find_corp(argv[0] if argv else sys.argv[1])
    rows = series(client, corp["corp_code"])
    print(f"# {corp['corp_name']}")
    print("\n".join(table_md(rows)))
    print("\n".join("- " + s for s in signals(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

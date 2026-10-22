"""
핵심 공시 유형 전용 파서
- 전환사채권발행결정 / 신주인수권부사채 / 교환사채  (메자닌)
- 유상증자결정
- 단일판매ㆍ공급계약체결

원리
  dart_client.extract_text() 가 만든 텍스트는  "라벨 | 라벨 | 값"  형태의 줄들이다.
  금감원 기업공시서식의 항목 라벨은 고정돼 있으므로, 라벨 정규식 → 바로 다음 셀을 값으로 읽는다.
  항목 라벨이 조금 달라도 잡히도록 정규식은 느슨하게, 못 찾은 필드는 None 으로 둔다(추측 금지).
  파생 수치(희석률, 리픽싱 하한 비율 등)는 원본 값이 전부 있을 때만 계산한다.

사용
  from parsers import parse_structured
  s = parse_structured(category, text)   # dict | None
"""
from __future__ import annotations

import re
from typing import Callable

# ------------------------------------------------------------------ 셀 스캐너
_LABEL_LIKE = re.compile(r"^\s*(\d{1,2}(-\d)?\.\s*)?[가-힣A-Za-z（）()ㆍ·/\s]{2,}(\([^)]*\))?\s*$")


_FIRST_ITEM = re.compile(
    r"^\s*1\.\s*(사채의\s*종류|신주의\s*종류|판매\s*[ㆍ·]?\s*공급계약\s*구분|변경\s*내용|취득\s*예정\s*주식|처분\s*예정\s*주식|"
    r"소각\s*할?\s*주식|소각\s*예정|계약\s*당사자|담보\s*제공자|계약\s*내용|계약\s*체결\s*기관|계약\s*해지\s*기관|"
    r"취득\s*예정\s*사채|매도\s*예정\s*사채|사채\s*취득|만기\s*전\s*취득|발행\s*회사|거래\s*상대방)")


def form_body(text: str) -> str:
    """
    정정공시는 앞에 '정정 전 | 정정 후' 표가 붙어 있어서 첫 매치가 '정정 전' 값을 집는 사고가 난다.
    서식의 1번 항목이 '마지막으로' 나오는 지점부터를 본문으로 본다 (정정표 안에도 1번 항목이 인용될 수 있으므로 last).
    """
    if not re.search(r"정\s*정\s*신\s*고|정정사항|정정관련", text[:3000]):
        return text
    lines = text.splitlines()
    last = None
    for i, ln in enumerate(lines):
        if _FIRST_ITEM.match(ln):
            last = i
    return "\n".join(lines[last:]) if last is not None else text


def _cells(text: str) -> list[tuple[int, str]]:
    """(줄번호, 셀) 목록. 빈 셀 제거. 정정공시면 본문만."""
    out = []
    for ln, line in enumerate(form_body(text).splitlines()):
        for c in line.split("|"):
            c = c.strip()
            if c:
                out.append((ln, c))
    return out


_PREFIX = r"^\s*(?:【)?(?:\d{1,2}(?:-\d)?\.\s*)?(?:-\s*)?(?:"   # "15. ", "2-1. ", "- " 같은 항목 번호 허용
_HAS_VALUE = re.compile(r"\d|^-$|해당")                             # 숫자/날짜/플레이스홀더가 들어 있으면 '값' 취급


def _find(cells: list[tuple[int, str]], label: str, *, after: int = 0, context: str | None = None,
          window: int = 6, numeric: bool = False, lookahead: int = 3) -> tuple[int | None, str | None]:
    """
    label 정규식(셀 시작에 앵커됨)에 맞는 셀을 찾아 (그 인덱스, 값) 반환.
    - context 가 주어지면 그 앞 window 셀 안에 context 정규식 매치가 있어야 한다.
    - numeric=True 면 다음 lookahead 셀 안에서 숫자/날짜/'-' 가 들어 있는 첫 셀을 값으로 삼는다.
      ("3. 증자전 발행주식총수 (주) | 보통주식 (주) | 24,000,000" 처럼 소제목 셀을 건너뛰기 위함)
    """
    lab = re.compile(_PREFIX + label + ")")
    ctx = re.compile(context) if context else None
    for i in range(after, len(cells)):
        _, c = cells[i]
        if not lab.search(c):
            continue
        if ctx and not any(ctx.search(cells[j][1]) for j in range(max(0, i - window), i)):
            continue
        if numeric:                       # "- 양수도 금액: 30,000,000,000원" 처럼 한 셀에 라벨:값
            m = re.search(r"[:：]\s*(.+)$", c)
            if m and re.search(r"\d", m.group(1)):
                return i, m.group(1).strip()
        if i + 1 >= len(cells):
            return i, None
        if not numeric:
            return i, cells[i + 1][1]
        for j in range(i + 1, min(i + 1 + lookahead, len(cells))):
            if _HAS_VALUE.search(cells[j][1]):
                return i, cells[j][1]
        return i, cells[i + 1][1]
    return None, None


# ------------------------------------------------------------------ 값 변환
def to_int(v: str | None) -> int | None:
    if v is None:
        return None
    s = v.replace(",", "").replace(" ", "")
    m = re.match(r"^-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(float(m.group(0)))
    except ValueError:
        return None


def to_float(v: str | None) -> float | None:
    if v is None:
        return None
    s = v.replace(",", "").replace(" ", "").replace("％", "%")
    m = re.match(r"^-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def to_date(v: str | None) -> str | None:
    if not v:
        return None
    m = re.search(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", v)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _is_placeholder(v: str | None) -> bool:
    return v is None or v.strip() in {"-", "", "해당사항없음", "해당사항 없음", "해당없음"}


def _clean(v: str | None) -> str | None:
    return None if _is_placeholder(v) else v.strip()


# ------------------------------------------------------------------ 메자닌 (CB/BW/EB)
def parse_convertible_bond(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731  텍스트 값
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731  숫자/날짜 값 (소제목 건너뜀)

    d: dict = {"_parser": "convertible_bond"}
    d["사채종류"] = _clean(g(r"종류$", context=r"사채의\s*종류", window=4)) or _clean(g(r"사채의\s*종류"))
    d["회차"] = to_int(n(r"회차$", context=r"사채의\s*종류", window=3))
    d["권면총액"] = to_int(n(r"권면\s*\(?전자등록\)?\s*총액|사채의\s*권면.*총액|권면총액"))
    d["표면이자율"] = to_float(n(r"표면\s*이자율"))
    d["만기이자율"] = to_float(n(r"만기\s*이자율"))
    d["만기일"] = to_date(n(r"사채\s*만기일"))
    d["발행방법"] = _clean(g(r"사채\s*발행\s*방법"))
    d["전환비율"] = to_float(n(r"전환\s*비율|행사\s*비율|교환\s*비율"))
    d["전환가액"] = to_int(n(r"전환\s*가액\s*\(?원|행사\s*가액\s*\(?원|교환\s*가액\s*\(?원|전환가액$|행사가액$|교환가액$"))
    d["발행주식수"] = to_int(n(r"주식수\s*\(?주\)?|주식수$", context=r"전환에\s*따라\s*발행할|행사.*발행할|교환.*주식"))
    d["주식총수대비비율"] = to_float(n(r"주식\s*총수\s*대비\s*비율"))
    i, start = _find(C, r"전환청구기간|행사기간|교환청구기간")
    if i is not None:
        d["청구기간_시작"] = to_date(_find(C, r"시작일", after=i)[1]) or to_date(start)
        d["청구기간_종료"] = to_date(_find(C, r"종료일", after=i)[1])
    d["최저조정가액"] = to_int(n(r"최저\s*조정\s*가액\s*\(?원|최저\s*조정\s*가액$"))
    d["리픽싱_근거"] = _clean(g(r"최저\s*조정\s*가액\s*근거"))
    d["청약일"] = to_date(n(r"청약일"))
    d["납입일"] = to_date(n(r"납입일"))
    d["이사회결의일"] = to_date(n(r"이사회\s*결의일"))
    d["증권신고서_제출대상"] = _clean(g(r"증권신고서\s*제출\s*대상\s*여부"))

    # 자금조달 목적 세부
    purpose = {}
    for key in ("시설자금", "영업양수자금", "운영자금", "채무상환자금", r"타법인\s*증권\s*취득자금", "기타자금"):
        v = to_int(n(key, context=r"자금\s*조달의?\s*목적", window=16))
        if v:
            purpose[key.replace("\\s*", "")] = v
    d["자금목적"] = purpose or None

    # 발행 대상자 (사모일 때)
    d["발행대상자"] = _parse_allottees(C, header=r"발행\s*대상자\s*명|대상자명|발행대상자", amount_label=r"발행\s*권면\s*총액|권면총액|배정")

    # 파생치
    if d["권면총액"] and d["전환가액"]:
        d["환산_전환주식수"] = d["권면총액"] // d["전환가액"]
    if d["전환가액"] and d["최저조정가액"]:
        d["리픽싱_하한비율"] = round(d["최저조정가액"] / d["전환가액"] * 100, 1)
        if d["권면총액"]:
            d["리픽싱후_최대주식수"] = d["권면총액"] // d["최저조정가액"]
    if d["주식총수대비비율"] and d.get("리픽싱_하한비율"):
        d["리픽싱후_최대비율"] = round(d["주식총수대비비율"] / (d["리픽싱_하한비율"] / 100), 1)
    d["사모여부"] = ("사모" in (d["발행방법"] or "")) or ("사모" in (d["사채종류"] or "")) or None
    return d


def _parse_allottees(C, header: str, amount_label: str) -> list[dict] | None:
    """'대상자명 | 관계 | 금액(또는 주식수)' 표를 찾아 행 단위로 읽기 (최대 10행)."""
    hi, _ = _find(C, header)
    if hi is None:
        return None
    hdr_line = C[hi][0]
    # 【...】 제목 줄에 걸렸으면(셀 1개), 그 다음에 오는 실제 헤더 행(셀 2개 이상, '관계' 포함)으로 이동
    if sum(1 for ln, _ in C if ln == hdr_line) < 2:
        for ln2 in sorted({ln for ln, _ in C if ln > hdr_line}):
            row = [c for ln, c in C if ln == ln2]
            if len(row) >= 2:
                if any("관계" in c or "대상자" in c for c in row):
                    hdr_line = ln2
                break
    rows = []
    lines: dict[int, list[str]] = {}
    for ln, c in C:
        if ln > hdr_line:
            lines.setdefault(ln, []).append(c)
    for ln in sorted(lines):
        line_cells = lines[ln]
        if len(line_cells) < 2:
            break
        if re.match(r"^\s*\d{1,2}\.", line_cells[0]):      # 다음 항목 번호 만나면 종료
            break
        if _is_placeholder(line_cells[0]):
            continue
        row = {"대상자": line_cells[0]}
        if len(line_cells) >= 2:
            row["관계"] = line_cells[1]
        nums = [to_int(x) for x in line_cells[1:] if to_int(x) is not None]
        if nums:
            row["금액또는주식수"] = nums[-1]
        rows.append(row)
        if len(rows) >= 10:
            break
    return rows or None


# ------------------------------------------------------------------ 유상증자
def parse_rights_offering(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731  텍스트 값
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731  숫자/날짜 값 (소제목 건너뜀)

    d: dict = {"_parser": "rights_offering"}
    d["신주_보통주"] = to_int(n(r"보통주(식)?\s*\(?주\)?|보통주(식)?$", context=r"신주의\s*종류와\s*수"))
    d["신주_기타주"] = to_int(n(r"(기타|우선)주(식)?\s*\(?주\)?|(기타|우선)주(식)?$", context=r"신주의\s*종류와\s*수"))
    d["액면가"] = to_int(n(r"1주당\s*액면가액"))
    d["증자전_발행주식총수"] = to_int(n(r"증자\s*전\s*발행주식\s*총수"))
    d["증자방식"] = _clean(g(r"증자\s*방식"))
    d["확정발행가_보통주"] = to_int(n(r"보통주(식)?\s*\(?원\)?|보통주(식)?$", context=r"확정\s*발행가", window=2))
    d["예정발행가_보통주"] = to_int(n(r"보통주(식)?\s*\(?원\)?|보통주(식)?$", context=r"예정\s*발행가", window=2))
    if d["확정발행가_보통주"] is None and d["예정발행가_보통주"] is None:
        d["확정발행가_보통주"] = to_int(n(r"보통주(식)?\s*\(?원", context=r"신주\s*발행가액", window=3))
    d["기준주가"] = to_float(n(r"보통주(식)?\s*\(?원", context=r"^\s*\d{1,2}\.\s*기준주가\s*$|기준주가\s*\|", window=2)) \
                 or to_float(n(r"기준주가\s*\(?원|기준주가$"))
    d["할인율_기재"] = to_float(n(r"(?:기준주가에\s*대한\s*|주당\s*액면가\s*대비\s*)?할인율"))
    d["신주배정기준일"] = to_date(n(r"(?:신주\s*)?배정\s*기준일"))
    d["납입일"] = to_date(n(r"납입일"))
    d["상장예정일"] = to_date(n(r"(?:신주의\s*)?상장\s*예정일"))
    d["이사회결의일"] = to_date(n(r"이사회\s*결의일"))
    d["증권신고서_제출대상"] = _clean(g(r"증권신고서\s*제출\s*대상\s*여부"))

    purpose = {}
    for key in ("시설자금", "영업양수자금", "운영자금", "채무상환자금", r"타법인\s*증권\s*취득자금", "기타자금"):
        v = to_int(n(key, context=r"자금\s*조달의?\s*목적", window=16))
        if v:
            purpose[key.replace("\\s*", "")] = v
    d["자금목적"] = purpose or None
    d["배정대상자"] = _parse_allottees(C, header=r"제3자배정\s*대상자|배정\s*대상자", amount_label=r"배정\s*주식수")

    # 파생치
    shares = (d["신주_보통주"] or 0) + (d["신주_기타주"] or 0) or None
    d["신주_합계"] = shares
    if shares and d["증자전_발행주식총수"]:
        d["희석률"] = round(shares / d["증자전_발행주식총수"] * 100, 1)
    price = d["확정발행가_보통주"] or d["예정발행가_보통주"]
    if price and d.get("기준주가"):
        d["기준주가대비"] = round((price / d["기준주가"] - 1) * 100, 2)      # 음수=할인, 양수=할증 (계산값)
        if d.get("할인율_기재") is not None and abs(abs(d["기준주가대비"]) - abs(d["할인율_기재"])) > 1.0:
            d["할인율_불일치"] = True                                      # 공시 기재 할인율과 계산값이 다름 → 원문 확인
    if shares and price:
        d["조달예정액"] = shares * price
        d["조달액_기준"] = "확정발행가" if d["확정발행가_보통주"] else "예정발행가"
    d["제3자배정"] = ("제3자" in (d["증자방식"] or "")) or ("3자" in (d["증자방식"] or "")) or None
    return d


# ------------------------------------------------------------------ 공급계약
def parse_supply_contract(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731  텍스트 값
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731  숫자/날짜 값 (소제목 건너뜀)

    d: dict = {"_parser": "supply_contract"}
    d["계약구분"] = _clean(g(r"판매\s*[ㆍ·]?\s*공급계약\s*구분"))
    d["계약명"] = _clean(g(r"체결\s*계약명|계약명|판매\s*[ㆍ·]?\s*공급계약\s*내용"))
    d["조건부계약여부"] = _clean(g(r"조건부\s*계약\s*여부"))
    d["확정계약금액"] = to_int(n(r"확정\s*계약금액"))
    d["조건부계약금액"] = to_int(n(r"조건부\s*계약금액"))
    d["계약금액총액"] = to_int(n(r"계약금액\s*총액")) or to_int(n(r"계약금액\s*\(?원", context=r"계약\s*내역", window=3))
    d["최근매출액"] = to_int(n(r"최근\s*매출액", context=r"계약\s*내역|계약금액", window=10)) or to_int(n(r"최근\s*매출액"))
    d["계약금_선급금"] = _clean(g(r"계약금\s*[ㆍ·]?\s*선급금\s*유무"))
    d["매출액대비"] = to_float(n(r"매출액\s*대비"))
    d["계약상대방"] = _clean(g(r"계약\s*상대방?"))
    d["회사와의관계"] = _clean(g(r"회사와의\s*관계"))
    d["공급지역"] = _clean(g(r"판매\s*[ㆍ·]?\s*공급\s*지역"))
    i, _ = _find(C, r"계약기간")
    if i is not None:
        d["계약기간_시작"] = to_date(_find(C, r"시작일", after=i)[1])
        d["계약기간_종료"] = to_date(_find(C, r"종료일", after=i)[1])
    d["주요계약조건"] = _clean(g(r"주요\s*계약\s*조건"))
    d["계약일자"] = to_date(n(r"계약\s*\(?수주\)?\s*일자|계약일자"))
    i, _ = _find(C, r"공시유보\s*관련\s*내용")
    d["공시유보_기한"] = _clean(_find(C, r"유보\s*기한", after=i or 0)[1]) if i is not None else None
    d["공시유보"] = _clean(_find(C, r"유보\s*사유", after=i or 0)[1]) if i is not None else _clean(g(r"유보\s*사유"))

    # 파생치
    if d["계약금액총액"] and d["최근매출액"] and d["매출액대비"] is None:
        d["매출액대비"] = round(d["계약금액총액"] / d["최근매출액"] * 100, 2)
    if d.get("계약기간_시작") and d.get("계약기간_종료"):
        from datetime import date
        y1, m1, d1 = map(int, d["계약기간_시작"].split("-"))
        y2, m2, d2 = map(int, d["계약기간_종료"].split("-"))
        days = (date(y2, m2, d2) - date(y1, m1, d1)).days
        d["계약기간_일수"] = days
        if d["계약금액총액"] and days >= 180:
            d["연환산_계약금액"] = int(d["계약금액총액"] * 365 / days)
    d["조건부"] = ("예" in (d["조건부계약여부"] or "")) or None
    return d


# ------------------------------------------------------------------ 라우팅

# ------------------------------------------------------------------ 최대주주 변경
def parse_major_shareholder_change(text: str) -> dict:
    """최대주주 변경 (거래소 공시 서식: 변경전/변경후 최대주주, 소유주식수, 지분율, 변경사유, 변경일자)."""
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731

    d: dict = {"_parser": "major_shareholder_change"}
    i_b, _ = _find(C, r"변경\s*전\s*최대주주|변경전")
    i_a, _ = _find(C, r"변경\s*후\s*최대주주|변경후")
    if i_b is not None:
        d["변경전_최대주주"] = _clean(_find(C, r"최대주주\s*등|최대주주\s*명|성명|명칭", after=i_b)[1]) or _clean(C[i_b + 1][1] if i_b + 1 < len(C) else None)
        d["변경전_주식수"] = to_int(_find(C, r"소유\s*주식수|주식수", after=i_b, numeric=True)[1])
        d["변경전_지분율"] = to_float(_find(C, r"소유\s*비율|지분율|비율", after=i_b, numeric=True)[1])
    if i_a is not None:
        d["변경후_최대주주"] = _clean(_find(C, r"최대주주\s*등|최대주주\s*명|성명|명칭", after=i_a)[1]) or _clean(C[i_a + 1][1] if i_a + 1 < len(C) else None)
        d["변경후_주식수"] = to_int(_find(C, r"소유\s*주식수|주식수", after=i_a, numeric=True)[1])
        d["변경후_지분율"] = to_float(_find(C, r"소유\s*비율|지분율|비율", after=i_a, numeric=True)[1])
    d["변경사유"] = _clean(g(r"변경\s*사유"))
    d["변경일자"] = to_date(n(r"변경\s*일자|최대주주\s*변경일"))
    d["주식양수도_금액"] = to_int(n(r"양수도\s*금액|거래\s*금액|취득\s*금액|인수\s*금액"))
    d["경영권_변동"] = _clean(g(r"경영권\s*변동\s*여부|경영권변동"))
    if d.get("변경후_지분율") is not None and d.get("변경전_지분율") is not None:
        d["지분율_변화"] = round(d["변경후_지분율"] - d["변경전_지분율"], 2)
    if d.get("주식양수도_금액") and d.get("변경후_주식수"):
        d["주당_인수가"] = d["주식양수도_금액"] // d["변경후_주식수"]
    return d


# ------------------------------------------------------------------ 자기주식
def parse_treasury_stock(text: str) -> dict:
    """자기주식 취득/처분/소각 결정 (직접 취득 및 신탁계약 포함)."""
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731

    d: dict = {"_parser": "treasury_stock"}
    head = text[:200]
    d["구분"] = ("소각" if "소각" in head else "처분" if "처분" in head else "신탁" if "신탁" in head else "취득" if "취득" in head else None)
    d["주식수_보통주"] = to_int(n(r"보통주식\s*\(?주\)?|보통주식$", context=r"(취득|처분|소각)\s*예정\s*주식|예정\s*주식|소각할\s*주식", window=8))
    d["주식수_기타주"] = to_int(n(r"기타주식\s*\(?주\)?|기타주식$", context=r"(취득|처분|소각)\s*예정\s*주식|예정\s*주식|소각할\s*주식", window=8))
    d["예정금액_보통주"] = to_int(n(r"보통주식\s*\(?원\)?|보통주식$", context=r"예정\s*금액|소각\s*예정\s*금액|계약\s*금액", window=8)) \
                        or to_int(n(r"계약\s*금액|예정\s*금액\s*\(?원"))
    i, _ = _find(C, r"(취득|처분)\s*예[정상]\s*기간|예[정상]\s*기간|소각\s*예정일|(해지\s*전\s*)?계약\s*기간")
    if i is not None:
        d["기간_시작"] = to_date(_find(C, r"시작일", after=i)[1]) or to_date(_find(C, r"소각\s*예정일", after=i, numeric=True)[1])
        d["기간_종료"] = to_date(_find(C, r"종료일", after=i)[1])
    d["목적"] = _clean(g(r"(취득|처분|소각|해지)\s*목적|계약\s*목적"))
    if d["구분"] == "신탁":
        d["계약체결기관"] = _clean(g(r"계약\s*(체결|해지)\s*기관|해지\s*기관"))
        d["해지예정일"] = to_date(n(r"해지\s*예정\s*일자?"))
        d["예정금액_보통주"] = to_int(n(r"계약\s*금액\s*\(?원|^계약금액$|해지\s*금액")) or d["예정금액_보통주"]
        d["주식수_보통주"] = to_int(n(r"보통주(식)?", context=r"(취득|해지)\s*예정\s*주식", window=2)) or d["주식수_보통주"]
        d["취득예정가격"] = to_int(n(r"보통주(식)?", context=r"취득하고자\s*하는\s*주식의\s*가격", window=2))
        d["방법"] = ("신탁계약 해지" if "해지" in text[:300] else "신탁계약 체결") + (f" ({d['계약체결기관']})" if d.get("계약체결기관") else "")
        d["해지사유"] = _clean(g(r"해지\s*사유")) if "해지" in text[:300] else None
    elif d["구분"] == "처분":
        # 6. 처분방법 | 시간외대량매매(주) | - / 장외처분(주) | 678 / 주주균등처분(주) | - → 숫자가 있는 소항목
        i, _ = _find(C, r"처분\s*방법")
        d["방법"] = None
        if i is not None:
            for j in range(i + 1, min(i + 12, len(C))):
                lab = C[j][1]
                if re.match(r"^\s*\d{1,2}\.", lab):
                    break
                if re.search(r"\(주\)|매매|처분", lab) and j + 1 < len(C) and to_int(C[j + 1][1]):
                    d["방법"] = re.sub(r"\(주\)\s*$", "", lab).strip()
                    break
    else:
        d["방법"] = _clean(g(r"(취득|소각)\s*방법"))
    d["발행주식총수_대비"] = to_float(n(r"발행주식\s*총수\s*대비|총수\s*대비\s*비율"))
    i2, _ = _find(C, r"(취득|소각)\s*전\s*자기주식\s*보유\s*현황|자기주식\s*보유\s*현황")
    if i2 is not None:
        d["보유_보통주_전"] = to_int(_find(C, r"보통주식", after=i2, numeric=True)[1])
    d["이사회결의일"] = to_date(n(r"이사회\s*결의일|(취득|처분|소각)\s*결정일|계약\s*체결\s*결정일"))
    d["위탁투자중개업자"] = _clean(g(r"위탁\s*투자중개업자|위탁투자중개업자"))
    shares = (d["주식수_보통주"] or 0) + (d["주식수_기타주"] or 0) or None
    d["주식수_합계"] = shares
    if shares and d.get("예정금액_보통주"):
        d["주당_예정단가"] = d["예정금액_보통주"] // shares
    return d


# ------------------------------------------------------------------ 감사보고서 / 감사의견
_OPINION_HEAD = re.compile(r"^\s*(적정\s*의견|한정\s*의견|부적정\s*의견|의견\s*거절)\s*$")


def parse_audit_report(text: str) -> dict:
    """
    감사보고서(원문) 또는 '감사보고서제출'(거래소 요약표) 양식.
    - 감사의견: 보고서 앞부분의 단독 제목줄(적정의견/한정의견/부적정의견/의견거절)을 최우선으로 읽음.
      거래소 요약표는 '감사의견 | 적정' 셀을 읽음.
    - 계속기업 불확실성: 감사인의 책임 보일러플레이트("…불확실성이 존재하는지 여부에 대하여 결론을 내립니다")는 제외하고,
      단락 제목 또는 "불확실성이 존재함을 나타냅니다" 류의 단정문이 있을 때만 True.
    """
    d: dict = {"_parser": "audit_report"}
    lines = text.splitlines()
    t = re.sub(r"\s+", " ", text)
    C = _cells(text)

    # 거래소 요약표 형식 (감사보고서제출)
    v = _find(C, r"감사\s*의견$|감사의견\s*등?$", context=r"감사의견|재무요건", window=4)[1]
    if v and re.fullmatch(r"(적정|한정|부적정|의견거절)", v.strip()):
        d["감사의견"] = v.strip()
        d["양식"] = "거래소요약표"
        gc = _find(C, r"계속기업\s*존속\s*불확실성\s*사유\s*해당\s*여부")[1]
        d["계속기업_불확실성"] = (gc.strip() == "해당") if gc else None
    else:
        d["양식"] = "감사보고서"
        for ln in lines[:400]:
            m = _OPINION_HEAD.match(ln)
            if m:
                d["감사의견"] = m.group(1).replace(" ", "").replace("의견", "") if "거절" not in m.group(1) else "의견거절"
                break
        if not d.get("감사의견"):
            m = re.search(r"감사\s*의견[^가-힣]{0,20}?(적정|한정|부적정|의견\s*거절)", t)
            if m:
                op = m.group(1).replace(" ", "")
                d["감사의견"] = "의견거절" if "거절" in op else op
        if not d.get("감사의견"):
            # '감사의견' 제목 뒤 의견 문단(~'감사의견근거' 또는 1,200자)만 보고 판정
            m = re.search(r"감사의견 (.{80,1200}?)(감사의견근거|의견근거|감사의견의 근거|$)", t)
            para = m.group(1) if m else ""
            if re.search(r"의견을\s*표명하지\s*(아니|않)", para):
                d["감사의견"] = "의견거절"
            elif re.search(r"공정하게\s*표시하고\s*있지\s*(아니|않)|공정하게\s*표시하지", para):
                d["감사의견"] = "부적정"
            elif re.search(r"제외하고는", para):
                d["감사의견"] = "한정"
            elif re.search(r"중요성의\s*관점에서\s*공정하게\s*표시", para):
                d["감사의견"] = "적정"
        gc_head = any(re.match(r"^\s*(독립된 감사인의 감사보고서\s*)?계속기업\s*관련\s*중요한\s*불확실성\s*$", ln) for ln in lines)
        gc_sent = bool(re.search(r"(중요한\s*불확실성이\s*존재함을\s*나타냅니다|불확실성이\s*존재합니다|유의적\s*의문을\s*제기(?!할\s*수)|계속기업가정에\s*대한\s*불확실성)", t))
        d["계속기업_불확실성"] = True if (gc_head or gc_sent) else None
    d["비적정"] = (d.get("감사의견") in {"한정", "부적정", "의견거절"}) if d.get("감사의견") else None
    d["강조사항"] = True if any(re.match(r"^\s*(독립된 감사인의 감사보고서\s*)?강조\s*사항\s*$", ln) for ln in lines) \
        and not re.search(r"강조사항\s*(해당사항\s*없음|없음)", t) else None
    d["핵심감사사항"] = True if any(re.match(r"^\s*핵심\s*감사\s*사항\s*$", ln) for ln in lines) else None
    firms = re.findall(r"([가-힣A-Za-z]{1,12}회계법인|회계법인\s*[가-힣A-Za-z]{2,8}|[가-힣]{2,10}\s*감사반)", t)
    firms = [f for f in firms if not re.search(r"^(해당|관련|외부|독립|이\s*)", f)]
    d["감사인"] = max(set(firms), key=firms.count) if firms else None
    m3 = re.search(r"제\s*(\d{1,3})\s*기", t)
    d["사업연도_기수"] = int(m3.group(1)) if m3 else None
    d["감사범위_제한"] = True if re.search(r"감사\s*범위\s*제한", t) else None
    d["재작성_언급"] = True if re.search(r"재무제표를\s*재작성|재작성된\s*재무제표|감사보고서를\s*재발행", t) else None
    return d


# ------------------------------------------------------------------ 자기 CB/BW 취득·매도
def parse_mezzanine_buyback(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731
    d: dict = {"_parser": "mezzanine_buyback"}
    head = text[:300]
    d["구분"] = "매도" if "매도" in head else "소각" if "소각" in head else "취득"
    d["사채종류"] = _clean(g(r"종류$", context=r"사채의\s*종류|취득\s*예정\s*사채|매도\s*예정\s*사채", window=6)) or _clean(g(r"사채의\s*종류"))
    d["회차"] = to_int(n(r"회차"))
    d["사채발행일"] = to_date(n(r"사채\s*발행\s*일자"))
    d["사채만기일"] = to_date(n(r"사채\s*만기일"))
    d["권면총액"] = to_int(n(r"사채의\s*권면\s*\(?전자등록\)?\s*총액"))
    d["취득대상_권면금액"] = to_int(n(r"(취득|매도|소각)\s*대상\s*사채의\s*권면"))
    d["취득금액"] = to_int(n(r"(취득|매도|소각)\s*금액"))
    d["취득일"] = to_date(n(r"(취득|매도|소각)\s*(결정|예정)?\s*일자?"))
    d["취득방법"] = _clean(g(r"(취득|매도|소각)\s*방법"))
    d["취득목적"] = _clean(g(r"만기\s*전\s*(취득|매도)\s*사유|(취득|매도|소각)\s*(사유|목적)"))
    d["처리방안"] = _clean(g(r"향후\s*처리\s*계획|(취득)?\s*후\s*처리\s*방안|처리\s*방안"))
    d["취득후_잔액"] = to_int(n(r"(취득|매도|소각)\s*후\s*사채의\s*권면.*잔액"))
    d["상대방"] = _clean(g(r"(취득|매도)\s*상대방"))
    if d.get("권면총액") and d.get("취득대상_권면금액"):
        d["취득비율"] = round(d["취득대상_권면금액"] / d["권면총액"] * 100, 1)
    if d.get("취득금액") and d.get("취득대상_권면금액"):
        d["권면대비_취득가"] = round(d["취득금액"] / d["취득대상_권면금액"] * 100, 2)
    return d


# ------------------------------------------------------------------ 최대주주 주식담보제공
def parse_pledge_contract(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731
    d: dict = {"_parser": "pledge_contract"}
    d["담보제공자"] = _clean(g(r"명칭\s*\(성명|명칭|성명", context=r"담보제공자", window=4))
    i, _ = _find(C, r"공시일\s*현재")
    if i is not None:
        d["현재_주식수"] = to_int(_find(C, r"소유\s*주식\s*수", after=i, numeric=True)[1])
        d["현재_지분율"] = to_float(_find(C, r"지분율", after=i, numeric=True)[1])
    j, _ = _find(C, r"담보권\s*전부\s*실행\s*시")
    if j is not None:
        d["실행시_주식수"] = to_int(_find(C, r"소유\s*주식\s*수", after=j, numeric=True)[1])
        d["실행시_지분율"] = to_float(_find(C, r"지분율", after=j, numeric=True)[1])
    d["채무금액_총액"] = to_int(n(r"채무\s*\(?차입\)?\s*금액\s*총액"))
    d["담보설정금액_총액"] = to_int(n(r"담보\s*설정\s*금액\s*총액"))
    d["누적_계약건수"] = to_int(n(r"누적\s*담보제공\s*계약\s*건수"))
    d["누적_담보주식수"] = to_int(n(r"누적\s*담보제공\s*주식\s*총수"))
    d["계약체결일"] = to_date(n(r"담보권\s*설정\s*계약\s*체결일"))
    if d.get("현재_지분율") is not None and d.get("실행시_지분율") is not None:
        d["실행시_지분율_하락"] = round(d["현재_지분율"] - d["실행시_지분율"], 2)
    if d.get("현재_주식수") and d.get("누적_담보주식수"):
        d["담보비율"] = round(d["누적_담보주식수"] / d["현재_주식수"] * 100, 1)
    return d


# ------------------------------------------------------------------ 최대주주 변경 수반 주식양수도계약
def parse_share_transfer_contract(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731
    d: dict = {"_parser": "share_transfer_contract"}
    i, _ = _find(C, r"양도인")
    if i is not None:
        d["양도인"] = _clean(C[i + 1][1]) if i + 1 < len(C) else None
        d["양도인_관계"] = _clean(_find(C, r"회사와의\s*관계", after=i)[1])
    j, _ = _find(C, r"양수인")
    if j is not None:
        d["양수인"] = _clean(C[j + 1][1]) if j + 1 < len(C) else None
        d["양수인_관계"] = _clean(_find(C, r"회사와의\s*관계", after=j)[1])
    d["양수도_주식수"] = to_int(n(r"양수도\s*주식수"))
    d["주당가액"] = to_int(n(r"1주당\s*가액"))
    d["양수도_대금"] = to_int(n(r"양수도\s*대금\s*\(?원"))
    d["계약일"] = to_date(n(r"계약\s*체결일|계약일"))
    d["변경예정일"] = to_date(n(r"변경\s*예정\s*일자|변경\s*예정일"))
    k, _ = _find(C, r"변경\s*예정\s*최대주주")
    if k is not None:
        d["변경예정_최대주주"] = _clean(_find(C, r"명칭|성명|최대주주\s*명", after=k)[1])
        d["변경후_지분율"] = to_float(_find(C, r"지분율|소유\s*비율", after=k, numeric=True)[1])
    d["잔금_언급"] = _clean(g(r"잔금"))
    if d.get("양수도_대금") and d.get("양수도_주식수") and not d.get("주당가액"):
        d["주당가액"] = d["양수도_대금"] // d["양수도_주식수"]
    return d


# ------------------------------------------------------------------ 타법인 주식 및 출자증권 취득/처분
def parse_equity_stake(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731
    d: dict = {"_parser": "equity_stake"}
    d["구분"] = "처분" if "처분" in text[:400] else "취득"
    d["대상회사"] = _clean(g(r"회사명\s*\(?국적\)?|회사명"))
    d["회사와관계"] = _clean(g(r"회사와\s*(의\s*)?관계"))
    d["주요사업"] = _clean(g(r"주요\s*사업"))
    d["주식수"] = to_int(n(r"(취득|처분)\s*주식수"))
    d["금액"] = to_int(n(r"(취득|처분)\s*금액"))
    d["자기자본"] = to_int(n(r"^자기자본\s*\(원\)|자기자본\(원\)"))
    d["자기자본대비"] = to_float(n(r"자기자본\s*대비"))
    d["후_주식수"] = to_int(n(r"소유\s*주식수", context=r"(취득|처분)\s*후", window=3))
    d["후_지분율"] = to_float(n(r"지분\s*비율", context=r"(취득|처분)\s*후", window=4))
    d["방법"] = _clean(g(r"(취득|처분)\s*방법"))
    d["목적"] = _clean(g(r"(취득|처분)\s*목적"))
    d["예정일"] = to_date(n(r"(취득|처분)\s*예정\s*일자"))
    d["자산총액대비"] = to_float(n(r"(취득|처분)\s*가액\s*/\s*자산\s*총액"))
    d["이사회결의일"] = to_date(n(r"이사회\s*결의일"))
    d["풋옵션"] = _clean(g(r"풋옵션\s*계약"))
    d["제3자배정_신주취득"] = _clean(g(r"제3자\s*배정에\s*의한\s*신주\s*취득"))
    if d.get("금액") and d.get("자기자본") and d.get("자기자본대비") is None:
        d["자기자본대비"] = round(d["금액"] / d["자기자본"] * 100, 2)
    if d.get("금액") and d.get("주식수"):
        d["주당가액"] = d["금액"] // d["주식수"]
    return d


# ------------------------------------------------------------------ 특수관계인에 대한 출자 (공정거래법, 단위 백만원)
def parse_affiliate_investment(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731
    d: dict = {"_parser": "affiliate_investment"}
    m = re.search(r"단위\s*[:：]\s*(백만원|천원|원)", text)        # 단위 표기는 본문 표 '위'에 있으므로 전체에서 찾음
    unit = {"백만원": 1_000_000, "천원": 1000, "원": 1}[m.group(1)] if m else 1_000_000   # 공정거래법 서식 기본 단위 = 백만원
    d["_단위"] = {1_000_000: "백만원", 1000: "천원", 1: "원"}[unit]
    d["상대방"] = _clean(g(r"거래\s*상대방"))
    d["관계"] = _clean(g(r"회사와의\s*관계"))
    d["출자일"] = to_date(n(r"(가\.\s*)?출자\s*일자"))
    d["출자목적물"] = _clean(g(r"(나\.\s*)?출자\s*목적물"))
    v = to_int(n(r"(다\.\s*)?출자\s*금액"))
    d["출자금액"] = v * unit if v is not None else None
    v = to_int(n(r"(라\.\s*)?출자\s*상대방\s*총\s*출자액"))
    d["총출자액"] = v * unit if v is not None else None
    d["목적"] = _clean(g(r"출자\s*목적$|^\s*3\.\s*출자\s*목적"))
    d["이사회결의일"] = to_date(n(r"이사회\s*의결일|이사회\s*결의일"))
    return d


# ------------------------------------------------------------------ 증권발행결과 (납입 완료 확인)
def parse_issuance_result(text: str) -> dict:
    C = _cells(text)
    g = lambda label, **kw: _find(C, label, **kw)[1]                 # noqa: E731
    n = lambda label, **kw: _find(C, label, numeric=True, **kw)[1]   # noqa: E731
    d: dict = {"_parser": "issuance_result"}
    d["증권종류"] = _clean(g(r"증권의\s*종류"))
    d["발행방법"] = _clean(g(r"발행\s*방법"))
    d["예정주식수"] = to_int(n(r"발행\s*예정\s*주식수"))
    d["예정금액"] = to_int(n(r"발행\s*예정\s*금액"))
    d["실제주식수"] = to_int(n(r"실제\s*발행\s*주식수"))
    d["실제금액"] = to_int(n(r"실제\s*발행\s*금액"))
    d["이사회결의일"] = to_date(n(r"(최초\s*)?이사회\s*결의일"))
    d["납입일"] = to_date(n(r"납입일"))
    m = re.search(r"제\s*(\d+)\s*회", text[:400])
    d["회차"] = int(m.group(1)) if m else None
    if d.get("예정금액") and d.get("실제금액") is not None:
        d["납입률"] = round(d["실제금액"] / d["예정금액"] * 100, 1)
    return d


# ------------------------------------------------------------------ 전환가액 조정 (리픽싱 실제 발생)
def parse_conversion_adjust(text: str) -> dict:
    """'1. 조정에 관한 사항 | 회차 | 상장여부 | 조정전 | 조정후 / 4 | 비상장 | 11,650 | 8,155' 처럼 머리줄 다음 데이터줄이 오는 표."""
    d: dict = {"_parser": "conversion_adjust", "조정": []}
    body = form_body(text)
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if re.search(r"조정\s*전\s*(전환|행사|교환)가액", ln) and i + 1 < len(lines):
            for row in lines[i + 1:i + 6]:
                cells = [c.strip() for c in row.split("|")]
                nums = [to_int(c) for c in cells]
                if len(cells) >= 3 and nums[0] is not None and nums[-1] is not None and nums[-2] is not None:
                    d["조정"].append({"회차": nums[0], "조정전": nums[-2], "조정후": nums[-1]})
                elif d["조정"]:
                    break
        if re.search(r"미전환사채의?\s*권면|미행사|조정\s*후\s*전환\s*가능", ln) and i + 1 < len(lines):
            for row in lines[i + 1:i + 6]:
                cells = [c.strip() for c in row.split("|")]
                nums = [to_int(c) for c in cells]
                if len(cells) >= 4 and nums[0] is not None and nums[-1] is not None:
                    rnd = nums[0]
                    face = next((x for x in nums[1:] if x and x > 1_000_000), None)
                    for r in d["조정"]:
                        if r["회차"] == rnd:
                            r.update({"미전환권면": face, "조정전_주식수": nums[-2], "조정후_주식수": nums[-1]})
                elif any("미전환권면" in r for r in d["조정"]):
                    break
    d["사유"] = _clean(_find(_cells(text), r"조정\s*사유")[1])
    d["조정일"] = to_date(_find(_cells(text), r"조정\s*(가액\s*)?적용일|조정일", numeric=True)[1])
    if not d["조정"]:
        d.pop("조정")
    return d


# ------------------------------------------------------------------ 철회 공시
def parse_withdrawal(text: str) -> dict:
    t = re.sub(r"\s+", " ", form_body(text))
    d: dict = {"_parser": "withdrawal"}
    d["대상"] = ("유상증자" if "유상증자" in t[:600] else "전환사채" if re.search(r"전환사채|CB", t[:600])
                 else "신주인수권부사채" if re.search(r"신주인수권부|BW", t[:600]) else None)
    m = re.search(r"보통주(?:식)?\s*[:：]?\s*([\d,]{3,})", t)
    d["신주"] = to_int(m.group(1)) if m else None
    m = re.search(r"최초\s*이사회\s*결의일\s*[:：]?\s*(\d{4}\s*[년.\-]\s*\d{1,2}\s*[월.\-]\s*\d{1,2})", t)
    d["최초결의일"] = to_date(m.group(1)) if m else None
    m = re.search(r"(\d+)\s*회차?", t[:800])
    d["회차"] = int(m.group(1)) if m and d["대상"] != "유상증자" else None
    m = re.search(r"철회\s*사유\s*[:：|]?\s*-?\s*(.{10,160}?)(?:\s\d\.\s|$)", t)
    d["사유"] = m.group(1).strip() if m else None
    return d


PARSERS: dict[str, Callable[[str], dict]] = {
    "메자닌발행": parse_convertible_bond,
    "메자닌취득": parse_mezzanine_buyback,
    "최대주주담보": parse_pledge_contract,
    "최대주주양수도": parse_share_transfer_contract,
    "타법인지분": parse_equity_stake,
    "발행결과": parse_issuance_result,
    "전환가액조정": parse_conversion_adjust,
    "철회": parse_withdrawal,
    "계열사출자": parse_affiliate_investment,
    "유상증자": parse_rights_offering,
    "공급계약": parse_supply_contract,
    "지배구조": parse_major_shareholder_change,
    "자기주식": parse_treasury_stock,
    "감사/외부감사": parse_audit_report,
}


def correction_info(text: str) -> dict:
    """정정공시 머리의 '정정사유' 들을 모아 반환. 철회/취소가 있으면 플래그."""
    head = text[:6000]
    if not re.search(r"정\s*정\s*신\s*고|정정사항|정정관련", head):
        return {}
    reasons = []
    for ln in head.splitlines():
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) >= 3 and re.search(r"정정\s*사유", cells[0]) is None and not re.search(r"정\s*정\s*전", ln):
            # '항목 | 정정사유 | 정정전 | 정정후' 데이터 행 → 두 번째 셀이 사유
            if len(cells) >= 4 and re.search(r"정정|변경|오류|철회|취소|추가|삭제|수정|기재|연장|누락|반영", cells[1]) and len(cells[1]) < 80:
                reasons.append(f"{cells[0]}: {cells[1]}")
        elif re.match(r"^\s*\d\.\s*정정\s*사유", ln) and len(cells) >= 2:
            reasons.append(cells[-1])
    reasons = [r for r in reasons if r and "정정사유" not in r][:6]
    out = {"_정정사유": reasons} if reasons else {}
    if re.search(r"철회|취소", " ".join(reasons) + head[:1500]):
        out["철회"] = True
    return out


def parse_structured(category: str, text: str) -> dict | None:
    fn = PARSERS.get(category)
    if not fn or not text:
        return None
    try:
        d = fn(text)
        d.update(correction_info(text))
    except Exception as e:  # noqa: BLE001 — 파서 실패가 파이프라인을 멈추면 안 됨
        return {"_parser": category, "_error": str(e)}
    filled = sum(1 for k, v in d.items() if not k.startswith("_") and v not in (None, {}, []))
    d["_filled"] = filled
    if d.get("철회"):
        return d
    return d if filled >= 2 else {**d, "_warning": "핵심 필드 대부분을 찾지 못함 — 양식 변형 가능성"}


# ------------------------------------------------------------------ 표시용
_WON = lambda n: f"{n:,}원" if isinstance(n, int) else "-"    # noqa: E731


def _won_h(n) -> str:
    if not isinstance(n, int):
        return "-"
    if abs(n) >= 10**8:
        return f"{n/10**8:,.0f}억원 ({n:,})"
    return f"{n:,}원"


def structured_lines(d: dict | None) -> list[str]:
    """리포트에 넣을 '핵심 수치' 줄 목록."""
    if not d or d.get("_error"):
        return []
    p = d.get("_parser")
    L = []
    if d.get("철회"):
        L.append("⚠ 발행/계약 철회 또는 취소 공시 — 아래 수치는 무효")
    if d.get("_정정사유"):
        L.append("정정사유: " + "; ".join(d["_정정사유"][:3]))
    if p == "convertible_bond":
        L += [f"권면총액 {_won_h(d['권면총액'])}" if d.get("권면총액") else "",
              f"발행방법 {d['발행방법']}" if d.get("발행방법") else "",
              f"전환가액 {_WON(d['전환가액'])}" + (f" → 최저 조정가액 {_WON(d['최저조정가액'])} (하한 {d['리픽싱_하한비율']}%)"
                                            if d.get("최저조정가액") else "") if d.get("전환가액") else "",
              f"전환 시 주식수 {d['발행주식수']:,}주" + (f" (총주식 대비 {d['주식총수대비비율']}%)" if d.get("주식총수대비비율") else "")
              if d.get("발행주식수") else (f"환산 전환주식수 {d['환산_전환주식수']:,}주" if d.get("환산_전환주식수") else ""),
              f"리픽싱 하한까지 조정 시 최대 {d['리픽싱후_최대주식수']:,}주" + (f" (총주식 대비 최대 약 {d['리픽싱후_최대비율']}%)"
                                                                   if d.get("리픽싱후_최대비율") else "") if d.get("리픽싱후_최대주식수") else "",
              (f"이자율 표면 {d['표면이자율']}%" + (f" / 만기 {d['만기이자율']}%" if d.get("만기이자율") is not None else ""))
              if d.get("표면이자율") is not None else "",
              f"전환청구기간 {d.get('청구기간_시작') or '?'} ~ {d.get('청구기간_종료') or '?'}" if d.get("청구기간_시작") else "",
              "자금목적 " + ", ".join(f"{k} {_won_h(v)}" for k, v in d["자금목적"].items()) if d.get("자금목적") else "",
              "발행대상자 " + ", ".join(f"{r['대상자']}({r.get('관계','')})" for r in d["발행대상자"][:5]) if d.get("발행대상자") else ""]
    elif p == "rights_offering":
        L += [f"증자방식 {d['증자방식']}" if d.get("증자방식") else "",
              f"신주 {d['신주_합계']:,}주 / 증자전 {d['증자전_발행주식총수']:,}주 → 희석률 {d['희석률']}%"
              if d.get("희석률") else (f"신주 {d['신주_합계']:,}주" if d.get("신주_합계") else ""),
              f"발행가 {_WON(d['확정발행가_보통주'] or d['예정발행가_보통주'])} ({d['조달액_기준']})" + (f" — 기준주가 {d['기준주가']:,.0f}원 대비 {abs(d['기준주가대비'])}% {'할인' if d['기준주가대비'] < 0 else '할증'}"
                 + (f" ⚠ 공시 기재 할인율 {d['할인율_기재']}%와 불일치, 원문 확인" if d.get("할인율_불일치") else "") if d.get("기준주가대비") is not None
                 else (f", 공시 기재 할인/할증율 {d['할인율_기재']}%" if d.get("할인율_기재") is not None else ""))
              if d.get("조달예정액") else "",
              f"조달예정액 {_won_h(d['조달예정액'])}" if d.get("조달예정액") else "",
              "자금목적 " + ", ".join(f"{k} {_won_h(v)}" for k, v in d["자금목적"].items()) if d.get("자금목적") else "",
              f"납입일 {d['납입일']}" + (f", 상장예정일 {d['상장예정일']}" if d.get("상장예정일") else "") if d.get("납입일") else "",
              "배정대상자 " + ", ".join(f"{r['대상자']}({r.get('관계','')})" for r in d["배정대상자"][:5]) if d.get("배정대상자") else ""]
    elif p == "supply_contract":
        L += [f"계약명 {d['계약명']}" if d.get("계약명") else "",
              f"계약금액 총액 {_won_h(d['계약금액총액'])}" + (f" = 최근 매출액 대비 {d['매출액대비']}%" if d.get("매출액대비") is not None else "")
              if d.get("계약금액총액") else "",
              f"조건부 계약: {d['조건부계약여부']}" + (f" (조건부 금액 {_won_h(d['조건부계약금액'])})" if d.get("조건부계약금액") else "")
              if d.get("조건부계약여부") else "",
              f"상대방 {d['계약상대방']}" + (f" ({d['회사와의관계']})" if d.get("회사와의관계") else "") if d.get("계약상대방") else "",
              f"계약기간 {d['계약기간_시작']} ~ {d['계약기간_종료']} ({d['계약기간_일수']}일" + (f", 연환산 {_won_h(d['연환산_계약금액'])}" if d.get("연환산_계약금액") else "") + ")"
              if d.get("계약기간_일수") else "",
              f"공급지역 {d['공급지역']}" if d.get("공급지역") else "",
              f"⚠ 공시유보: {d['공시유보']}" if d.get("공시유보") else ""]
    elif p == "major_shareholder_change":
        b, a = d.get("변경전_최대주주"), d.get("변경후_최대주주")
        L += [f"최대주주 {b or '?'} → {a or '?'}" if (b or a) else "",
              f"지분율 {d.get('변경전_지분율')}% → {d.get('변경후_지분율')}%" + (f" ({d['지분율_변화']:+}%p)" if d.get("지분율_변화") is not None else "")
              if d.get("변경후_지분율") is not None else "",
              f"변경후 보유주식 {d['변경후_주식수']:,}주" if d.get("변경후_주식수") else "",
              f"양수도 금액 {_won_h(d['주식양수도_금액'])}" + (f" (주당 약 {d['주당_인수가']:,}원)" if d.get("주당_인수가") else "") if d.get("주식양수도_금액") else "",
              f"변경사유 {d['변경사유']}" if d.get("변경사유") else "",
              f"경영권 변동 {d['경영권_변동']}" if d.get("경영권_변동") else "",
              f"변경일 {d['변경일자']}" if d.get("변경일자") else ""]
    elif p == "treasury_stock":
        L += [f"자기주식 {d['구분']}" if d.get("구분") else "",
              f"주식수 {d['주식수_합계']:,}주" + (f" (발행주식총수 대비 {d['발행주식총수_대비']}%)" if d.get("발행주식총수_대비") is not None else "") if d.get("주식수_합계") else "",
              f"예정금액 {_won_h(d['예정금액_보통주'])}" + (f" (주당 약 {d['주당_예정단가']:,}원)" if d.get("주당_예정단가") else "") if d.get("예정금액_보통주") else "",
              f"기간 {d.get('기간_시작') or '?'} ~ {d.get('기간_종료') or '?'}" if d.get("기간_시작") else "",
              f"목적 {d['목적']}" if d.get("목적") else "",
              f"방법 {d['방법']}" if d.get("방법") else "",
              f"취득 전 보유 {d['보유_보통주_전']:,}주" if d.get("보유_보통주_전") else ""]
    elif p == "mezzanine_buyback":
        L += [f"자기 사채 {d['구분']}" + (f" ({d['사채종류']})" if d.get("사채종류") else "") + (f" 제{d['회차']}회차" if d.get("회차") else ""),
              f"{d['구분']} 대상 권면 {_won_h(d['취득대상_권면금액'])}" + (f" (발행총액 {_won_h(d['권면총액'])}의 {d['취득비율']}%)" if d.get("취득비율") is not None else "")
              if d.get("취득대상_권면금액") else "",
              f"{d['구분']}금액 {_won_h(d['취득금액'])}" + (f" (권면 대비 {d['권면대비_취득가']}%)" if d.get("권면대비_취득가") else "") if d.get("취득금액") else "",
              f"{d['구분']}후 잔액 {_won_h(d['취득후_잔액'])}" if d.get("취득후_잔액") is not None else "",
              f"{d['구분']}일 {d['취득일']}" if d.get("취득일") else "",
              f"사유 {d['취득목적']}" if d.get("취득목적") else "",
              f"처리방안 {d['처리방안']}" if d.get("처리방안") else "",
              f"상대방 {d['상대방']}" if d.get("상대방") else ""]
    elif p == "pledge_contract":
        L += [f"담보제공자(최대주주) {d['담보제공자']}" if d.get("담보제공자") else "",
              f"현재 지분율 {d['현재_지분율']}% → 담보권 전부 실행 시 {d['실행시_지분율']}% ({-d['실행시_지분율_하락']:+}%p)"
              if d.get("실행시_지분율_하락") is not None else (f"현재 지분율 {d['현재_지분율']}%" if d.get("현재_지분율") is not None else ""),
              f"담보제공 주식 {d['누적_담보주식수']:,}주 (보유분의 {d['담보비율']}%)" if d.get("담보비율") is not None else "",
              f"채무금액 {_won_h(d['채무금액_총액'])} / 담보설정금액 {_won_h(d['담보설정금액_총액'])}" if d.get("채무금액_총액") else "",
              f"누적 계약 {d['누적_계약건수']}건" if d.get("누적_계약건수") else "",
              f"계약체결일 {d['계약체결일']}" if d.get("계약체결일") else ""]
    elif p == "share_transfer_contract":
        L += [f"양도인 {d['양도인']}" + (f" ({d['양도인_관계']})" if d.get("양도인_관계") else "") if d.get("양도인") else "",
              f"양수인 {d['양수인']}" + (f" ({d['양수인_관계']})" if d.get("양수인_관계") else "") if d.get("양수인") else "",
              f"양수도 {d['양수도_주식수']:,}주 · 대금 {_won_h(d['양수도_대금'])}" + (f" (주당 {d['주당가액']:,}원)" if d.get("주당가액") else "")
              if d.get("양수도_주식수") and d.get("양수도_대금") else "",
              f"변경 후 예정 지분율 {d['변경후_지분율']}%" if d.get("변경후_지분율") is not None else "",
              f"계약일 {d['계약일']}" + (f" · 변경예정일 {d['변경예정일']}" if d.get("변경예정일") else "") if d.get("계약일") else ""]
    elif p == "equity_stake":
        L += [f"{d['대상회사']}" + (f" ({d['회사와관계']})" if d.get("회사와관계") else "") + f" 지분 {d['구분']}" if d.get("대상회사") else "",
              f"{d['구분']}금액 {_won_h(d['금액'])}" + (f" = 자기자본 대비 {d['자기자본대비']}%" if d.get("자기자본대비") is not None else "")
              if d.get("금액") else "",
              f"{d['주식수']:,}주" + (f" (주당 약 {d['주당가액']:,}원)" if d.get("주당가액") else "") if d.get("주식수") else "",
              f"{d['구분']} 후 지분율 {d['후_지분율']}%" + (f" ({d['후_주식수']:,}주)" if d.get("후_주식수") else "") if d.get("후_지분율") is not None else "",
              f"목적 {d['목적']}" if d.get("목적") else "",
              f"방법 {d['방법']}" + (f" · 예정일 {d['예정일']}" if d.get("예정일") else "") if d.get("방법") else "",
              f"⚠ 풋옵션 계약: {d['풋옵션']}" if d.get("풋옵션") and d["풋옵션"] not in ("아니오", "미해당", "해당없음") else ""]
    elif p == "affiliate_investment":
        L += [f"출자 상대방 {d['상대방']}" + (f" ({d['관계']})" if d.get("관계") else "") if d.get("상대방") else "",
              f"출자금액 {_won_h(d['출자금액'])}" + (f" (누적 총출자액 {_won_h(d['총출자액'])})" if d.get("총출자액") else "") if d.get("출자금액") else "",
              f"목적물 {d['출자목적물']}" if d.get("출자목적물") else "",
              f"목적 {d['목적']}" if d.get("목적") else "",
              f"출자일 {d['출자일']}" if d.get("출자일") else ""]
    elif p == "issuance_result":
        L += [f"{d.get('발행방법') or d.get('증권종류') or '증권'} 발행 완료" + (f" (제{d['회차']}회)" if d.get("회차") else ""),
              f"실제 {_won_h(d['실제금액'])}" + (f" / 예정 {_won_h(d['예정금액'])} = {d['납입률']}%" if d.get("납입률") is not None else "")
              if d.get("실제금액") is not None else "",
              f"주식수 {d['실제주식수']:,}주" if d.get("실제주식수") else "",
              f"납입일 {d['납입일']}" + (f" · 최초 결의 {d['이사회결의일']}" if d.get("이사회결의일") else "") if d.get("납입일") else ""]
    elif p == "conversion_adjust":
        for r in d.get("조정", [])[:3]:
            L.append(f"제{r['회차']}회 전환가 {r['조정전']:,} → {r['조정후']:,}원"
                     + (f", 전환가능 주식 {r['조정전_주식수']:,} → {r['조정후_주식수']:,}주" if r.get("조정후_주식수") else "")
                     + (f" (미전환 {_won_h(r['미전환권면'])})" if r.get("미전환권면") else ""))
        L.append(f"사유 {d['사유']}" if d.get("사유") else "")
    elif p == "withdrawal":
        L += [f"⚠ {d['대상'] or ''} 결정 철회" + (f" (최초 결의 {d['최초결의일']})" if d.get("최초결의일") else ""),
              f"철회 대상 신주 {d['신주']:,}주" if d.get("신주") else "",
              f"사유 {d['사유'][:80]}" if d.get("사유") else ""]
    elif p == "audit_report":
        L += [f"감사의견 {d['감사의견']}" + (" ⚠ 비적정 — 상장폐지 사유 가능" if d.get("비적정") else "") if d.get("감사의견") else "감사의견 미확인(원문 확인 필요)",
              "⚠ 계속기업 관련 중요한 불확실성" if d.get("계속기업_불확실성") else "",
              "강조사항 있음" if d.get("강조사항") else "",
              "⚠ 감사범위 제한 언급" if d.get("감사범위_제한") else "",
              "⚠ 재무제표 재작성 / 감사보고서 재발행" if d.get("재작성_언급") else "",
              f"감사인 {d['감사인']}" if d.get("감사인") else "",
              f"제{d['사업연도_기수']}기" if d.get("사업연도_기수") else ""]
    return [x for x in L if x]


# ------------------------------------------------------------------ 한 줄 요약 (표 칸용)
_HEADLINE_KEYS = {
    "convertible_bond": ("리픽싱 하한까지", "전환 시 주식수", "환산 전환주식수", "권면총액"),
    "rights_offering": ("신주 ", "발행가", "조달예정액"),
    "supply_contract": ("계약금액 총액", "계약명"),
    "pledge_contract": ("현재 지분율", "담보제공 주식"),
    "share_transfer_contract": ("양수도 ", "양수인"),
    "major_shareholder_change": ("최대주주 ", "지분율"),
    "treasury_stock": ("자기주식", "예정금액"),
    "mezzanine_buyback": ("대상 권면", "사유"),
    "audit_report": ("감사의견", "계속기업"),
    "equity_stake": ("취득금액", "처분금액", "취득 후", "처분 후"),
    "affiliate_investment": ("출자금액", "출자 상대방"),
    "issuance_result": ("실제 ", "납입일"),
    "conversion_adjust": ("제", "사유"),
    "withdrawal": ("⚠", "철회 대상"),
}


def headline(d: dict | None, n: int = 2) -> str:
    """핵심표 '한 줄' 칸: 유형별 가장 중요한 수치 줄을 골라 n개. 철회면 철회만."""
    lines = [x for x in structured_lines(d) if not x.startswith("정정사유")]
    if not lines:
        return ""
    if lines[0].startswith("⚠ 발행/계약 철회"):
        return "⚠ 철회/취소 공시"
    keys = _HEADLINE_KEYS.get((d or {}).get("_parser"), ())
    picked = []
    for k in keys:
        for x in lines:
            if x.startswith(k) and x not in picked:
                picked.append(x)
                break
        if len(picked) >= n:
            break
    for x in lines:                       # 모자라면 앞에서 채움
        if len(picked) >= n:
            break
        if x not in picked:
            picked.append(x)
    return " / ".join(picked)

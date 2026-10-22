"""
DART(전자공시) Open API 클라이언트
- 기업 고유번호(corp_code) 조회
- 공시 목록 조회
- 공시 원문 다운로드 + 텍스트 추출
- 단일회사 전체 재무제표 조회

API 키 발급: https://opendart.fss.or.kr  (무료, 일 20,000건)
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import requests

BASE = "https://opendart.fss.or.kr/api"
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
CACHE_DIR = Path(os.environ.get("DART_CACHE_DIR", ".dart_cache"))

# 정기보고서 코드
REPRT_Q1, REPRT_HALF, REPRT_Q3, REPRT_ANNUAL = "11013", "11012", "11014", "11011"


class DartError(RuntimeError):
    pass


@dataclass
class Disclosure:
    corp_code: str
    corp_name: str
    stock_code: str
    report_nm: str
    rcept_no: str
    flr_nm: str          # 제출인
    rcept_dt: str        # 접수일자 YYYYMMDD
    rm: str              # 비고 (유/코/정 등)

    @property
    def url(self) -> str:
        return VIEWER.format(rcept_no=self.rcept_no)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["url"] = self.url
        return d


class DartClient:
    def __init__(self, api_key: str | None = None, cache_dir: Path = CACHE_DIR, min_interval: float = 0.15):
        self.api_key = api_key or os.environ.get("DART_API_KEY")
        if not self.api_key:
            raise DartError("DART_API_KEY 가 없습니다. .env 또는 환경변수로 설정하세요.")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self._last_call = 0.0
        self._corp_table: list[dict] | None = None

    # ------------------------------------------------------------------ 공통
    def _get(self, endpoint: str, **params) -> requests.Response:
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        params["crtfc_key"] = self.api_key
        last_err = None
        for attempt in range(4):                       # 일시적 끊김/5xx 대비 재시도 (1s, 3s, 9s)
            try:
                r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=30)
                self._last_call = time.time()
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code}", response=r)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                last_err = e
                if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code < 500:
                    raise
                time.sleep(3 ** attempt)
        raise DartError(f"{endpoint}: 네트워크 재시도 4회 실패 — {last_err}")

    def _get_json(self, endpoint: str, **params) -> dict:
        data = self._get(endpoint, **params).json()
        status = data.get("status")
        if status not in ("000", "013"):          # 013 = 조회 결과 없음
            raise DartError(f"{endpoint}: [{status}] {data.get('message')}")
        return data

    # ------------------------------------------------------------- 기업코드
    def corp_table(self) -> list[dict]:
        """전체 기업 고유번호 테이블 (하루 1회 캐시)."""
        if self._corp_table is not None:
            return self._corp_table
        cache = self.cache_dir / f"corpcode_{date.today():%Y%m%d}.json"
        if cache.exists():
            self._corp_table = json.loads(cache.read_text("utf-8"))
            return self._corp_table

        r = self._get("corpCode.xml")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            xml_bytes = z.read(z.namelist()[0])
        root = ET.fromstring(xml_bytes)
        rows = []
        for el in root.iter("list"):
            rows.append({
                "corp_code": (el.findtext("corp_code") or "").strip(),
                "corp_name": (el.findtext("corp_name") or "").strip(),
                "stock_code": (el.findtext("stock_code") or "").strip(),
                "modify_date": (el.findtext("modify_date") or "").strip(),
            })
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
        self._corp_table = rows
        return rows

    def find_corp(self, name_or_code: str, listed_only: bool = True) -> dict:
        """회사명(정확/부분) 또는 6자리 종목코드로 기업 찾기."""
        q = name_or_code.strip()
        table = self.corp_table()
        if re.fullmatch(r"\d{6}", q):
            for row in table:
                if row["stock_code"] == q:
                    return row
            raise DartError(f"종목코드 {q} 를 찾을 수 없습니다.")

        cands = [r for r in table if r["corp_name"] == q]
        if not cands:
            cands = [r for r in table if q in r["corp_name"]]
        if listed_only:
            listed = [r for r in cands if r["stock_code"]]
            cands = listed or cands
        if not cands:
            raise DartError(f"'{q}' 에 해당하는 회사를 찾을 수 없습니다.")
        if len(cands) > 1:
            names = ", ".join(f"{c['corp_name']}({c['stock_code'] or '비상장'})" for c in cands[:8])
            raise DartError(f"'{q}' 후보가 여러 개입니다. 정확한 이름이나 종목코드로 다시 지정하세요: {names}")
        return cands[0]

    # ------------------------------------------------------------- 공시목록
    def list_disclosures(self, corp_code: str, bgn_de: str, end_de: str | None = None,
                         pblntf_ty: str | None = None) -> list[Disclosure]:
        """
        pblntf_ty: A=정기공시 B=주요사항보고 C=발행공시 D=지분공시 E=기타공시
                   F=외부감사관련 G=펀드공시 H=자산유동화 I=거래소공시 J=공정위공시
        """
        end_de = end_de or f"{date.today():%Y%m%d}"
        out: list[Disclosure] = []
        page = 1
        while True:
            params = dict(bgn_de=bgn_de, end_de=end_de, page_no=page, page_count=100)
            if corp_code:                      # 비우면 시장 전체 공시
                params["corp_code"] = corp_code
            if pblntf_ty:
                params["pblntf_ty"] = pblntf_ty
            data = self._get_json("list.json", **params)
            for row in data.get("list", []):
                out.append(Disclosure(
                    corp_code=row.get("corp_code", ""), corp_name=row.get("corp_name", ""),
                    stock_code=row.get("stock_code", ""), report_nm=re.sub(r"\s+", " ", row.get("report_nm", "")).strip(),
                    rcept_no=row.get("rcept_no", ""), flr_nm=row.get("flr_nm", ""),
                    rcept_dt=row.get("rcept_dt", ""), rm=row.get("rm", "")))
            if page >= int(data.get("total_page", 1) or 1):
                break
            page += 1
        return out

    # ------------------------------------------------------------- 공시원문
    def fetch_document_text(self, rcept_no: str, max_chars: int = 60_000) -> str:
        """공시 원문(zip 안 XML)을 받아 태그 제거 후 평문 반환. 캐시됨."""
        raw_cache = self.cache_dir / f"doc_{rcept_no}.raw"
        if raw_cache.exists():
            raw = raw_cache.read_bytes()
        else:
            r = self._get("document.xml", rcept_no=rcept_no)
            try:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    names = sorted(z.namelist(), key=lambda n: -z.getinfo(n).file_size)
                    raw = z.read(names[0])
            except zipfile.BadZipFile:
                # 에러 응답은 json/xml 평문으로 옴
                raise DartError(f"원문 다운로드 실패 rcept_no={rcept_no}: {r.text[:200]}")
            raw_cache.parent.mkdir(parents=True, exist_ok=True)
            raw_cache.write_bytes(raw)
        return extract_text(raw)[:max_chars]

    # ------------------------------------------------------------- 재무제표
    def financials(self, corp_code: str, bsns_year: int, reprt_code: str = REPRT_ANNUAL,
                   fs_div: str = "CFS") -> list[dict]:
        """단일회사 전체 재무제표. fs_div: CFS=연결, OFS=별도. 없으면 OFS로 재시도."""
        data = self._get_json("fnlttSinglAcntAll.json", corp_code=corp_code,
                              bsns_year=str(bsns_year), reprt_code=reprt_code, fs_div=fs_div)
        rows = data.get("list", [])
        if not rows and fs_div == "CFS":
            data = self._get_json("fnlttSinglAcntAll.json", corp_code=corp_code,
                                  bsns_year=str(bsns_year), reprt_code=reprt_code, fs_div="OFS")
            rows = data.get("list", [])
        return rows


# ---------------------------------------------------------------------- 유틸
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u3000]+")
_NL = re.compile(r"\n{3,}")


def _strip_inline(fragment: str) -> str:
    """셀 내부: 모든 태그를 공백으로 (셀 안의 <P>/<BR>는 줄바꿈이 아니라 띄어쓰기로)."""
    return _TAG.sub(" ", fragment)


def extract_text(raw: bytes) -> str:
    """
    DART 원문 XML(TD/TH/TE/TU 셀) 또는 KIND HTML에서 텍스트 추출.
    표 한 행 = 한 줄, 셀 구분 = ' | '. 원본의 줄바꿈/들여쓰기 공백은 먼저 전부 접는다.
    """
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            s = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        s = raw.decode("utf-8", errors="ignore")
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"\s+", " ", s)                                   # 원본 줄바꿈·들여쓰기 제거
    # 셀 단위로 먼저 처리 (DART: TD/TH/TE/TU, KIND: td/th)
    s = re.sub(r"(?is)<(td|th|te|tu)\b[^>]*>(.*?)</\1\s*>",
               lambda m: " " + _strip_inline(m.group(2)).strip() + " | ", s)
    s = re.sub(r"(?i)</tr\s*>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h\d|title|table)\s*>|<br\s*/?>", "\n", s)
    s = _TAG.sub(" ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    s = _WS.sub(" ", s)
    lines = []
    for line in s.split("\n"):
        line = re.sub(r"\s*\|\s*", " | ", line).strip(" |")
        line = re.sub(r"(\s*\|\s*){2,}", " | ", line)          # 빈 셀 연속 제거
        lines.append(line.strip())
    s = "\n".join(lines)
    s = _NL.sub("\n\n", s)
    return s.strip()


def yyyymmdd(d: date) -> str:
    return f"{d:%Y%m%d}"


def days_ago(n: int) -> str:
    return yyyymmdd(date.today() - timedelta(days=n))


KEY_ACCOUNTS = ["매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계",
                "영업활동현금흐름", "유동자산", "유동부채", "현금및현금성자산"]


def summarize_financials(rows: Iterable[dict]) -> list[dict]:
    """핵심 계정만 골라 당기/전기/전전기 금액을 표로."""
    out, seen = [], set()
    for r in rows:
        nm = (r.get("account_nm") or "").replace(" ", "")
        for key in KEY_ACCOUNTS:
            if nm.startswith(key) and key not in seen:
                seen.add(key)
                out.append({
                    "계정": key,
                    "재무제표": r.get("sj_nm"),
                    "당기": r.get("thstrm_amount"),
                    "전기": r.get("frmtrm_amount"),
                    "전전기": r.get("bfefrmtrm_amount"),
                    "당기명": r.get("thstrm_nm"),
                })
    return out

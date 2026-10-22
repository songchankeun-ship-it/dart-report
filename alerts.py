"""
신규 공시 실시간 알림 (관심종목 → 텔레그램)

  python alerts.py --once --dry-run        # 콘솔로만 출력 (테스트)
  python alerts.py --once                  # 한 번 확인하고 종료 (작업 스케줄러에 5~10분 간격으로 걸기 권장)
  python alerts.py --interval 600          # 데몬처럼 600초마다 반복
  python alerts.py --once --min-importance 4 --llm

환경변수(.env)
  DART_API_KEY        필수
  TELEGRAM_BOT_TOKEN  @BotFather 에서 발급
  TELEGRAM_CHAT_ID    봇과 대화 시작 후 https://api.telegram.org/bot<TOKEN>/getUpdates 에서 chat.id 확인
  ANTHROPIC_API_KEY   --llm 쓸 때만

상태 파일 .alert_state.json 에 이미 보낸 접수번호를 기록해 중복 전송을 막는다.
처음 실행 시에는 '오늘 이전' 공시를 전부 기전송 처리해서 과거 공시가 한꺼번에 쏟아지지 않게 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("dart.env")   # 윈도우 탐색기에서 보이는 이름
except ImportError:
    pass

import requests

from dart_client import DartClient, DartError, Disclosure, days_ago
from analyzer import classify, build_fact_pack, interpret
from parsers import structured_lines
from pipeline import load_watchlist

STATE = Path(".alert_state.json")
ICON = {"긍정적": "🟢", "부정적": "🔴", "중립": "⚪", "판단보류": "🟡"}


# ------------------------------------------------------------------ 상태
def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text("utf-8"))
    return {"seen": [], "corp_codes": {}}


def save_state(st: dict) -> None:
    st["seen"] = st["seen"][-5000:]                     # 무한 증식 방지
    STATE.write_text(json.dumps(st, ensure_ascii=False), "utf-8")


# ------------------------------------------------------------------ 텔레그램
def send_telegram(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없어 전송을 건너뜁니다.", file=sys.stderr)
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=20)
    if r.status_code != 200:
        print("텔레그램 전송 실패:", r.text[:200], file=sys.stderr)
        return False
    return True


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_alert(d: Disclosure, importance: int, category: str, is_corr: bool,
                 s_lines: list[str], analysis: dict | None) -> str:
    sig = f"{ICON.get(analysis['signal'], '')} {analysis['signal']}" if analysis else ""
    head = [f"<b>{'★' * importance} {_esc(d.corp_name)}</b> {'⚠정정 ' if is_corr else ''}{sig}".rstrip(),
            f"{_esc(d.report_nm)}",
            f"{d.rcept_dt[4:6]}/{d.rcept_dt[6:]} · {category} · 제출 {_esc(d.flr_nm)}"]
    body = [f"• {_esc(x)}" for x in s_lines[:5]]
    if analysis:
        body.append(f"\n<b>{_esc(analysis['headline'])}</b>")
        if analysis.get("watch_points"):
            body.append("확인: " + _esc("; ".join(analysis["watch_points"][:2])))
        v = analysis.get("_verification", {})
        if v and not v.get("passed", True):
            body.append("⚠ 자동검증 경고(원문과 불일치 발견) — 해석 신뢰도 낮음")
    tail = [f'<a href="{d.url}">DART 원문</a>',
            "<i>사실 정리 자료이며 투자 권유가 아닙니다.</i>"]
    return "\n".join(head + ([""] if body else []) + body + [""] + tail)


# ------------------------------------------------------------------ 폴링 1회
def check_once(client: DartClient, corps: list[str], st: dict, *, min_importance: int,
               use_llm: bool, dry_run: bool, lookback_days: int) -> int:
    first_run = not st["seen"]
    bgn = days_ago(lookback_days)
    end = f"{date.today():%Y%m%d}"
    seen = set(st["seen"])
    sent = 0

    for q in corps:
        code = st["corp_codes"].get(q)
        if not code:
            try:
                code = client.find_corp(q)["corp_code"]
                st["corp_codes"][q] = code
            except DartError as e:
                print(f"[{q}] {e}", file=sys.stderr)
                continue
        try:
            discs = client.list_disclosures(code, bgn, end)
        except (DartError, requests.RequestException) as e:
            print(f"[{q}] 조회 실패: {e}", file=sys.stderr)
            continue

        for d in sorted(discs, key=lambda x: x.rcept_no):
            if d.rcept_no in seen:
                continue
            seen.add(d.rcept_no)
            if first_run and d.rcept_dt < end:
                continue                                   # 첫 실행: 과거 공시는 기전송 처리만
            c = classify(d)
            if c.importance < min_importance:
                continue
            try:
                text = client.fetch_document_text(d.rcept_no)
            except DartError:
                text = ""
            pack = build_fact_pack(d, text)
            analysis = None
            if use_llm and text:
                try:
                    analysis = interpret(pack)
                except Exception as e:  # noqa: BLE001
                    print("  해석 실패:", e, file=sys.stderr)
            msg = format_alert(d, c.importance, c.category, c.is_correction,
                               structured_lines(pack.structured), analysis)
            if dry_run:
                print("-" * 60 + "\n" + msg + "\n")
                sent += 1
            elif send_telegram(msg):
                sent += 1
            time.sleep(0.3)

    st["seen"] = sorted(seen)
    save_state(st)
    return sent


def main(argv=None):
    ap = argparse.ArgumentParser(description="관심종목 신규 공시 텔레그램 알림")
    ap.add_argument("--watchlist", default="watchlist.txt")
    ap.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    ap.add_argument("--interval", type=int, default=600, help="반복 간격(초), --once 없을 때")
    ap.add_argument("--min-importance", type=int, default=3)
    ap.add_argument("--llm", action="store_true", help="알림에 LLM 한 줄 해석 포함")
    ap.add_argument("--dry-run", action="store_true", help="전송 대신 콘솔 출력")
    ap.add_argument("--lookback-days", type=int, default=3, help="조회 범위(일). 하루 1회 실행이면 3 권장")
    args = ap.parse_args(argv)

    try:
        corps = load_watchlist(args.watchlist)
        client = DartClient()
    except (FileNotFoundError, DartError) as e:
        print("오류:", e, file=sys.stderr)
        return 1

    st = load_state()
    while True:
        t0 = time.strftime("%H:%M:%S")
        n = check_once(client, corps, st, min_importance=args.min_importance, use_llm=args.llm,
                       dry_run=args.dry_run, lookback_days=args.lookback_days)
        print(f"[{t0}] 확인 완료 — 알림 {n}건")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())

"""
티켓링크 서버 시간 실시간 동기화 도구

원리:
- HTTP HEAD 요청으로 받은 Date 응답 헤더는 1초 단위이므로 단순 읽기만으로는 ±500ms 오차가 있음
- 연속 요청을 보내면서 Date 값이 바뀌는 "경계 순간"을 포착하면 ms 단위까지 정밀 동기화 가능
- 이후에는 로컬 시계(time.time)에 측정된 오프셋을 더해 서버 시간을 표시

사용법:
  python server_time.py                       # 티켓링크 서버 시간 실시간 표시
  python server_time.py --host example.com    # 다른 도메인
  python server_time.py --target 20:00:00     # 오늘 20:00:00 까지 카운트다운
"""

import argparse
import http.client
import socket
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))
DEFAULT_HOST = "www.ticketlink.co.kr"


def fetch_date(conn: http.client.HTTPSConnection):
    """HEAD 요청 한 번. (date_str, local_sent, local_recv) 반환."""
    t0 = time.time()
    conn.request("HEAD", "/")
    resp = conn.getresponse()
    t1 = time.time()
    date_str = resp.getheader("Date")
    resp.read()
    return date_str, t0, t1


def sync(host: str, max_samples: int = 80, interval: float = 0.08):
    """
    서버 Date 헤더 경계 탐지로 로컬-서버 시간 오프셋(초)을 산출.
    여러 경계 후보 중 불확정 구간(prev_recv ~ cur_sent)이 가장 짧은 것을 선택.
    반환: (offset_sec, uncertainty_sec)  offset 정의: server = local + offset
    """
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, timeout=5, context=ctx)
    best_offset = None
    best_uncertainty = float("inf")
    prev_date = None
    prev_recv = None
    sys.stdout.write(f"[sync] {host} 서버 시간 동기화 중")
    sys.stdout.flush()
    for i in range(max_samples):
        try:
            date_str, sent, recv = fetch_date(conn)
        except (http.client.HTTPException, socket.error, ssl.SSLError):
            try:
                conn.close()
            except Exception:
                pass
            conn = http.client.HTTPSConnection(host, timeout=5, context=ctx)
            prev_date = None
            continue
        if not date_str:
            break

        if prev_date is not None and date_str != prev_date:
            # 서버 시계가 prev_recv ~ sent 사이 어느 시점에 date_str 로 넘어갔음
            server_epoch = parsedate_to_datetime(date_str).timestamp()
            midpoint_local = (prev_recv + sent) / 2.0
            uncertainty = (sent - prev_recv) / 2.0
            offset = server_epoch - midpoint_local
            if uncertainty < best_uncertainty:
                best_uncertainty = uncertainty
                best_offset = offset
                sys.stdout.write(f"\n  경계 포착: 오프셋 {offset*1000:+.1f}ms  불확정 ±{uncertainty*1000:.1f}ms")
            else:
                sys.stdout.write(".")
            if best_uncertainty < 0.010:  # 10ms 이내면 충분
                break
        else:
            sys.stdout.write(".")
        sys.stdout.flush()
        prev_date = date_str
        prev_recv = recv
        time.sleep(interval)
    try:
        conn.close()
    except Exception:
        pass
    print()
    if best_offset is None:
        raise RuntimeError("서버 시간 동기화 실패")
    return best_offset, best_uncertainty


def format_kst(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, KST)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{dt.microsecond // 1000:03d}"


def run_clock(offset: float, target_epoch: float | None):
    """서버 시간을 실시간 표시. target_epoch 가 있으면 카운트다운과 알람."""
    fired = False
    try:
        while True:
            server_now = time.time() + offset
            line = f"서버시간(KST): {format_kst(server_now)}"
            if target_epoch is not None:
                remain = target_epoch - server_now
                line += f"  |  목표까지: {remain:+8.3f}s"
                if not fired and remain <= 0:
                    # 터미널 벨 + 시각적 알람
                    sys.stdout.write("\a")
                    fired = True
                    line += "  <<< 지금! >>>"
            sys.stdout.write("\r" + line + "   ")
            sys.stdout.flush()
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n종료")


def parse_target(s: str, server_now_epoch: float) -> float:
    """'HH:MM:SS' 또는 'YYYY-MM-DD HH:MM:SS' → epoch(서버 KST 기준). 이미 지난 시각이면 다음 날."""
    s = s.strip()
    now_dt = datetime.fromtimestamp(server_now_epoch, KST)
    try:
        if len(s) <= 8:
            hh, mm, ss = [int(x) for x in s.split(":")]
            target = now_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if target <= now_dt:
                target += timedelta(days=1)
        else:
            target = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except ValueError as e:
        raise SystemExit(f"--target 형식 오류: {e} (예: 20:00:00 또는 '2026-04-24 20:00:00')")
    return target.timestamp()


def main():
    ap = argparse.ArgumentParser(description="서버 시간 실시간 동기화 (HTTP Date 헤더 기반)")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"대상 호스트 (기본: {DEFAULT_HOST})")
    ap.add_argument("--target", default=None, help="목표 시각 'HH:MM:SS' 또는 'YYYY-MM-DD HH:MM:SS' (KST)")
    ap.add_argument("--resync", type=float, default=0, help="N초마다 재동기화 (0이면 1회만)")
    args = ap.parse_args()

    offset, unc = sync(args.host)
    print(f"[완료] 오프셋 = {offset*1000:+.2f}ms  (불확정 ±{unc*1000:.1f}ms)")

    target_epoch = None
    if args.target:
        server_now = time.time() + offset
        target_epoch = parse_target(args.target, server_now)
        print(f"[목표] {format_kst(target_epoch)} (KST)")

    print("Ctrl+C 로 종료\n")

    if args.resync > 0:
        # 주기적 재동기화 스레드
        import threading
        stop = threading.Event()

        state = {"offset": offset}

        def resyncer():
            while not stop.wait(args.resync):
                try:
                    new_off, _ = sync(args.host)
                    state["offset"] = new_off
                except Exception:
                    pass

        t = threading.Thread(target=resyncer, daemon=True)
        t.start()

        try:
            while True:
                server_now = time.time() + state["offset"]
                line = f"서버시간(KST): {format_kst(server_now)}"
                if target_epoch is not None:
                    remain = target_epoch - server_now
                    line += f"  |  목표까지: {remain:+8.3f}s"
                sys.stdout.write("\r" + line + "   ")
                sys.stdout.flush()
                time.sleep(0.01)
        except KeyboardInterrupt:
            stop.set()
            print("\n종료")
    else:
        run_clock(offset, target_epoch)


if __name__ == "__main__":
    main()

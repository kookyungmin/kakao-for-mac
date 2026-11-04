import argparse
import curses
import locale
import sys

from .core import KakaoBackend, DemoBackend, BackendError, room_name_key, background_hint
from .ui import App


def main():
    parser = argparse.ArgumentParser(description="Sheetdesk — 로컬 카카오톡 터미널 화면")
    parser.add_argument("room_name", nargs="?", help="표시할 대화방 이름 (생략하면 김도완)")
    parser.add_argument("--demo", action="store_true", help="실제 카톡에 접근하지 않는 샘플 모드")
    parser.add_argument("--readonly", action="store_true", help="메시지 전송 비활성화")
    parser.add_argument("--cli", help="kakaocli 실행 파일 경로")
    parser.add_argument("--interval", type=float, default=2.0, help="대화 폴링 간격(초), 기본 2")
    parser.add_argument("--view", choices=("console", "sheet", "log"), default="console", help="기본 화면 (console)")
    parser.add_argument("--room", dest="rooms", action="append", help="표시할 방 이름 (공백 제외 완전 일치, 기본: 김도완)")
    parser.add_argument("--doctor", action="store_true", help="설치 및 DB 읽기 권한 진단(메시지 내용 출력 없음)")
    args = parser.parse_args()
    args.rooms = ([args.room_name] if args.room_name is not None else []) + (args.rooms or [])
    if any(not name.strip() for name in args.rooms):
        parser.error("대화방 이름은 비워둘 수 없습니다.")
    args.rooms = args.rooms or ["김도완"]
    if args.interval < 1:
        parser.error("--interval 은 1초 이상이어야 합니다.")
    backend = DemoBackend() if args.demo else KakaoBackend(args.cli, args.readonly)
    if args.doctor:
        try:
            rooms = backend.rooms()
            allowed = {room_name_key(name) for name in args.rooms}
            matches = [room for room in rooms if room_name_key(room.name) in allowed]
            print("카톡 DB 조회 성공 · 지정된 방 {}개 (내용은 출력하지 않음)".format(len(matches)))
            if not args.demo:
                diagnostics = backend.diagnostics(matches[0].name if matches else args.rooms[0])
                print("카톡 실행: {}".format("예" if diagnostics.get("running") else "아니오"))
                print("손쉬운 사용 권한: {}".format("허용" if diagnostics.get("accessibility") else "없음 — 실행 터미널을 시스템 설정에서 허용하세요"))
                if diagnostics.get("accessibility"):
                    print("채팅 목록 접근: {}".format("가능" if diagnostics.get("chat_list_available") else "카톡 메인 채팅 목록 창을 여세요"))
                    if "exact_room_visible" in diagnostics:
                        print("지정된 방 완전 일치: {}".format("확인" if diagnostics["exact_room_visible"] else "현재 카톡 목록에서 찾지 못함"))
                print("백그라운드 전송 준비: {}".format("완료" if diagnostics.get("background_ready") else background_hint(diagnostics.get("background_reason", ""))))
            return 0
        except BackendError as error:
            print("진단 실패: {}".format(error), file=sys.stderr)
            print("실행 터미널에 전체 디스크 접근 권한을 부여하고 다시 실행하세요.", file=sys.stderr)
            return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("대화형 터미널에서 실행하세요. 설치 확인은 --doctor, 샘플 화면은 --demo 입니다.")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(lambda screen: App(screen, backend, args).run())
    except KeyboardInterrupt:
        pass
    except curses.error:
        print("터미널 화면을 열지 못했습니다. TERM 설정과 창 크기를 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

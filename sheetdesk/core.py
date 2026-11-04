"""CLI adapter and presentation model. Never logs or persists conversation data."""
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unicodedata


class BackendError(Exception):
    pass


def room_name_key(name):
    """Ignore spacing only for the display allowlist; sending uses the original name."""
    return "".join(unicodedata.normalize("NFC", str(name)).split())


def background_hint(reason):
    hints = {
        "accessibility_permission_denied": "손쉬운 사용 권한 없음: 실행 터미널을 시스템 설정에서 허용하고 재시작하세요.",
        "background_app_not_running": "카톡이 실행 중이어야 합니다. 앱이 자동 실행하지 않습니다.",
        "background_window_required": "대상 방을 카톡에서 한 번 연 뒤 터미널로 돌아오세요. 방을 자동으로 열지 않습니다.",
        "background_multiple_windows": "대화 창이 여러 개입니다. 대상 대화 창만 남겨야 백그라운드로 전송할 수 있습니다.",
        "background_target_mismatch": "열린 대화 창이 선택한 방과 다릅니다. 대상 방만 열어 주세요.",
        "background_window_minimized": "대화 창의 최소화를 풀고 터미널 뒤에 두세요. 자동으로 창을 복원하지 않습니다.",
        "background_target_not_main": "대상 대화 창의 포커스 자동 지정에 실패했습니다. 창 상태를 다시 확인합니다.",
        "background_input_not_focused": "백그라운드 입력 포커스를 지정하지 못했습니다. 수정된 kakaocli 버전을 확인하세요.",
        "background_focus_failed": "입력 포커스 자동 지정에 실패했습니다. 카톡 UI가 백그라운드 포커스를 허용하지 않는 상태입니다.",
        "background_cannot_hide": "카톡 숨김을 확인하지 못해 중단했습니다. 창을 앞으로 띄우는 전송은 하지 않습니다.",
        "background_became_visible": "카톡의 숨김 상태가 풀려 다시 숨기고 중단했습니다. 자동 재전송하지 않습니다.",
        "background_existing_draft": "이전 실행 파일의 초안 차단입니다. 앱을 종료하고 수정본으로 다시 실행하세요.",
        "background_set_value_failed": "백그라운드 입력이 지원되지 않거나 실패했습니다. 원래 카톡 입력창을 확인하세요.",
        "background_target_changed": "전송 직전 대상 창이 바뀌어 중단했습니다. 카톡 입력창을 확인하세요.",
        "background_event_failed": "백그라운드 키 이벤트를 만들지 못했습니다. 자동 재전송하지 않습니다.",
        "background_delivery_unconfirmed": "전송 결과 미확인: 원래 카톡에서 확인하세요. 자동 재전송하지 않습니다.",
    }
    return next((text for code, text in hints.items() if code in str(reason)), "백그라운드 전송 조건을 확인하지 못했습니다. --doctor로 진단하세요.")


def safe_text(value):
    """Remove terminal controls, including escape sequences and bidi overrides."""
    result = []
    for char in str(value if value is not None else ""):
        if char == "\n":
            result.append(" ↵ ")
        elif char == "\t":
            result.append("    ")
        elif not unicodedata.category(char).startswith("C"):
            result.append(char)
    return "".join(result)


def width(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def clip(text, columns):
    text = safe_text(text)
    result, used = [], 0
    for char in text:
        size = width(char)
        if used + size > max(0, columns):
            break
        result.append(char)
        used += size
    return "".join(result)


def wrap(text, columns):
    text = safe_text(text)
    columns = max(2, columns)
    lines, line, used = [], [], 0
    for char in text:
        size = width(char)
        if used + size > columns:
            lines.append("".join(line))
            line, used = [], 0
        line.append(char)
        used += size
    lines.append("".join(line))
    return lines


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    unread: int = 0
    members: int = 0
    kind: str = "unknown"

    @classmethod
    def parse(cls, item):
        return cls(str(item["id"]), str(item.get("display_name") or "(unknown)"),
                   int(item.get("unread_count") or 0),
                   int(item.get("member_count") or 0), str(item.get("type", "unknown")))


@dataclass(frozen=True)
class Message:
    id: str
    timestamp: str
    sender: str
    text: str
    mine: bool = False

    @classmethod
    def parse(cls, item):
        mine = bool(item.get("is_from_me", False))
        body = item.get("text")
        return cls(str(item["id"]), str(item.get("timestamp") or ""),
                   "나" if mine else str(item.get("sender") or item.get("sender_id") or "?"),
                   str(body) if body is not None else "[{}]".format(item.get("type", "첨부")), mine)

    @property
    def clock(self):
        try:
            return datetime.fromisoformat(self.timestamp.replace("Z", "+00:00")).astimezone().strftime("%m/%d %H:%M")
        except (ValueError, OverflowError):
            return safe_text(self.timestamp)[:16]


def ordered_messages(items):
    # IDs can be 64-bit values; keep them intact (no floating-point conversion).
    def order(msg):
        return (msg.timestamp, int(msg.id) if msg.id.isdigit() else 0)
    unique = {str(item["id"]): Message.parse(item) for item in items}
    return sorted(unique.values(), key=order)


class KakaoBackend:
    def __init__(self, executable=None, readonly=False):
        root = Path(__file__).resolve().parent.parent
        bundled = root / "vendor/kakaocli/.build/release/kakaocli"
        candidate = executable or os.environ.get("SHEETDESK_CLI")
        self.executable = str(candidate or (bundled if bundled.is_file() else shutil.which("kakaocli") or bundled))
        self.readonly = readonly
        self.account_cache = root / ".state/account.json"
        self._send_lock = threading.Lock()
        self._last_send = float("-inf")

    def run(self, args, timeout=15, private=False, input_text=None):
        try:
            options = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
            result = subprocess.run([self.executable] + list(args), capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                    shell=False,
                                    env=dict(os.environ, SHEETDESK_ACCOUNT_CACHE=str(self.account_cache)), **options)
        except FileNotFoundError:
            raise BackendError("kakaocli가 없습니다. 먼저 ./setup.sh 를 실행하세요.") from None
        except subprocess.TimeoutExpired:
            hint = "전송 결과를 확인하지 못했습니다. 카톡에서 확인 후 재전송하세요." if private else "조회 시간이 초과됐습니다. 권한과 카톡 상태를 확인하세요."
            raise BackendError(hint) from None
        except OSError:
            raise BackendError("kakaocli 실행에 실패했습니다. 경로와 실행 권한을 확인하세요.") from None
        if result.returncode:
            # Send errors can echo the message. Never display those details.
            if private:
                detail = (result.stderr + result.stdout).lower()
                if "background_" in detail:
                    raise BackendError(background_hint(detail))
                if "accessibility_permission_denied" in detail:
                    raise BackendError("손쉬운 사용 권한 없음: 시스템 설정 → 개인정보 보호 및 보안 → 손쉬운 사용에서 실행 터미널을 허용하고 재시작하세요.")
                if "no open windows" in detail:
                    raise BackendError("카톡 메인 창을 찾지 못했습니다. 카톡의 채팅 목록 창을 한 번 열고 다시 시도하세요.")
                if "not found in the chat list" in detail:
                    raise BackendError("카톡 화면에서 이름이 같은 방을 찾지 못했습니다. 채팅 목록의 방 이름과 이름 변경 여부를 확인하세요.")
                if "input field" in detail:
                    raise BackendError("카톡 입력창을 찾지 못했습니다. 카톡 버전/UI 호환 문제입니다. 자동 재전송하지 않습니다.")
                raise BackendError("전송 실패: 카톡에서 실제 전송 여부와 접근성 권한을 확인하세요. 자동 재시도하지 않습니다.")
            detail = safe_text(result.stderr.strip() or result.stdout.strip())
            raise BackendError(clip(detail, 240) or "kakaocli 조회 실패")
        return result.stdout

    def json(self, args):
        try:
            data = json.loads(self.run(args))
        except (ValueError, TypeError):
            raise BackendError("kakaocli JSON 응답을 읽지 못했습니다. 버전 호환성을 확인하세요.") from None
        if not isinstance(data, list) or not all(isinstance(row, dict) and "id" in row for row in data):
            raise BackendError("예상하지 못한 kakaocli 응답 형식입니다.")
        return data

    def rooms(self):
        return [Room.parse(row) for row in self.json(["chats", "--json", "--limit", "100000"])]

    def diagnostics(self, room_name=None, hide=False):
        args = ["diagnostics"]
        if hide:
            args.append("--hide")
        if room_name:
            args += ["--chat", room_name]
        try:
            result = json.loads(self.run(args))
        except ValueError:
            raise BackendError("진단 응답을 읽지 못했습니다.") from None
        if not isinstance(result, dict):
            raise BackendError("진단 응답 형식이 잘못됐습니다.")
        return result

    def hide(self):
        status = self.diagnostics(hide=True)
        if not status.get("hidden"):
            raise BackendError(background_hint("background_cannot_hide"))
        return status

    def messages(self, room_id, limit=200):
        return ordered_messages(self.json(["messages", "--chat-id", str(room_id), "--limit", str(limit), "--json"]))

    def validate_target(self, room):
        if not room.name.strip() or room.name.lower() in ("(unknown)", "unknown"):
            raise BackendError("이름 없는 방은 전송할 수 없습니다. 카톡에서 방 이름을 지정하세요.")
        current = self.rooms()
        matches = [r for r in current if r.name == room.name]
        if len(matches) != 1 or matches[0].id != room.id:
            raise BackendError("방 이름이 중복되거나 변경됐습니다. 카톡에서 고유한 방 이름을 지정하고 새로고침하세요.")

    def prepare(self, room, text):
        if self.readonly:
            raise BackendError("읽기 전용 모드입니다.")
        if not text.strip():
            raise BackendError("보낼 내용을 입력하세요.")
        if len(text) > 4000 or "\x00" in text:
            raise BackendError("메시지는 4,000자 이하로 입력하세요. NUL 문자는 사용할 수 없습니다.")
        self.validate_target(room)
        diagnostics = self.diagnostics(room.name, hide=True)
        if not diagnostics.get("background_ready"):
            raise BackendError(background_hint(diagnostics.get("background_reason", "")))
        if not diagnostics.get("hidden"):
            raise BackendError(background_hint("background_cannot_hide"))
        # The '--' also prevents messages beginning with '-' from becoming CLI flags.
        self.run(["send", "--dry-run", "--exact", "--background", "--message-stdin", "--", room.name], private=True, input_text=text)

    def send(self, room, text):
        if self.readonly:
            raise BackendError("읽기 전용 모드입니다.")
        with self._send_lock:
            if time.monotonic() - self._last_send < 2:
                raise BackendError("전송 간격은 최소 2초입니다. 잠시 뒤 다시 시도하세요.")
            self.validate_target(room)
            # Refuse unpatched binaries instead of silently using substring matching.
            help_text = self.run(["send", "--help"])
            if "--exact" not in help_text or "--message-stdin" not in help_text or "--background" not in help_text:
                raise BackendError("안전한 전송에는 동봉한 kakaocli가 필요합니다. ./setup.sh 를 실행하세요.")
            self._last_send = time.monotonic()
            self.run(["send", "--exact", "--background", "--message-stdin", "--", room.name], timeout=15, private=True, input_text=text)


class DemoBackend:
    """Entirely synthetic. Never starts kakaocli or touches KakaoTalk data."""
    readonly = False

    def __init__(self):
        self._messages = {}
        self._counter = 100

    def rooms(self):
        return [Room("1", "김도완", 2, 2, "direct"),
                Room("2", "프로젝트", 0, 4, "group"), Room("3", "연습장", 0, 1, "direct")]

    def messages(self, room_id, limit=200):
        if room_id not in self._messages:
            now = datetime.now().astimezone().isoformat()
            self._messages[room_id] = [
                Message("1", now, "샘플", "이 화면의 대화는 실제 카톡이 아닌 샘플입니다."),
                Message("2", now, "나", "콘솔에서 한글과 긴 메시지도 볼 수 있습니다.", True),
                Message("3", now, "샘플", "내용을 입력하고 Enter를 누르면 바로 전송합니다."),
            ]
        return list(self._messages[room_id][-limit:])

    def prepare(self, room, text):
        if not text.strip():
            raise BackendError("보낼 내용을 입력하세요.")

    def send(self, room, text):
        self.messages(room.id)
        self._counter += 1
        now = datetime.now().astimezone().isoformat()
        self._messages[room.id].append(Message(str(self._counter), now, "나", text, True))
        self._counter += 1
        self._messages[room.id].append(Message(str(self._counter), now, "샘플", "샘플 답장: " + text))

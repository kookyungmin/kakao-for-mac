"""Responsive curses UI. Worker results are tagged by room; no data leaves this process."""
from concurrent.futures import ThreadPoolExecutor
import curses
from datetime import datetime
import queue
import time

from .core import BackendError, clip, safe_text, width, wrap, room_name_key
from . import __version__


class App:
    def __init__(self, screen, backend, args):
        self.screen, self.backend, self.args = screen, backend, args
        self.rooms, self.messages = [], []
        self.allowed_names = frozenset(room_name_key(name) for name in (getattr(args, "rooms", None) or []))
        self.room_picker = False
        self.selected = 0
        self.room_id = None
        self.filter = ""
        self.drafts = {}
        self.sending_drafts = {}
        self.draft, self.cursor = "", 0
        self.mode = "browse"
        self.focus = "rooms"
        self.view = args.view
        self.cover = False
        self.help = False
        self.offset = 0
        self.status = "연결 중…" if not args.demo else "샘플 모드 · 실제 카톡에 접근하지 않습니다"
        self.error = False
        self.error_kind = None
        self.last_refresh = None
        self.last_poll, self.last_rooms = float("-inf"), float("-inf")
        self.pending = set()
        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.running = True
        self.dirty = True

    @property
    def visible_rooms(self):
        return [r for r in self.rooms
                if (self.room_picker or not self.allowed_names or room_name_key(r.name) in self.allowed_names)
                and self.filter.casefold() in r.name.casefold()]

    @property
    def room(self):
        return next((r for r in self.rooms if r.id == self.room_id), None)

    def job(self, kind, context, fn):
        if kind in self.pending:
            return
        self.pending.add(kind)

        def worker():
            try:
                self.events.put((kind, context, fn(), None))
            except BackendError as error:
                self.events.put((kind, context, {"delivery_uncertain": error.delivery_uncertain}, str(error)))
            except Exception:
                # Never print a traceback with conversation data in locals/arguments.
                self.events.put((kind, context, {"delivery_uncertain": kind == "send"}, "처리 중 오류가 발생했습니다. 새로고침하거나 다시 실행하세요."))
        self.pool.submit(worker)

    def note(self, text, error=False):
        self.status, self.error, self.dirty = text, error, True

    def choose(self, room):
        # CLI names choose the initial conversation, not a permanent allowlist.
        # Subsequent choices come from the current room list (including Ctrl+O).
        if not any(candidate.id == room.id for candidate in self.rooms):
            return
        self.allowed_names = frozenset([room_name_key(room.name)])
        if self.room_id == room.id:
            self.prepare_room(room)
            return
        if self.room_id is not None:
            self.drafts[self.room_id] = self.draft
        self.room_id = room.id
        self.draft = self.drafts.get(room.id, "")
        self.cursor = len(self.draft)
        self.messages, self.offset = [], 0
        self.mode = "compose" if self.view == "console" and not self.args.readonly else "browse"
        if self.view == "console":
            self.focus = "messages"
        self.last_poll = float("-inf")
        self.note("불러오는 중…")
        self.prepare_room(room)

    def prepare_room(self, room):
        if not self.args.demo and not self.args.readonly:
            self.mode = "opening"
            self.note("카톡 대화창 준비 중…")
            self.job("open_room", room.id, lambda: self.backend.focus_room(room))

    def collect(self):
        while True:
            try:
                kind, context, result, error = self.events.get_nowait()
            except queue.Empty:
                return
            self.pending.discard(kind)
            if kind == "open_room" and context != self.room_id:
                continue
            if kind == "messages" and context != self.room_id:
                continue
            if error:
                if kind == "send" and (result or {}).get("delivery_uncertain"):
                    self.sending_drafts.pop(context, None)
                    self.mode = "compose"
                    self.last_poll = float("-inf")
                    self.error_kind = None
                    self.note("")
                    continue
                self.note(error, True)
                self.error_kind = kind
                if kind == "send":
                    text = self.sending_drafts.pop(context, "")
                    if not (result or {}).get("delivery_uncertain"):
                        self.drafts[context] = text
                        if context == self.room_id:
                            self.draft, self.cursor = text, len(text)
                    self.mode = "compose"
                elif kind == "open_room":
                    self.mode = "compose"
                continue
            if kind == "rooms":
                old_id = self.room_id
                before = self.visible_rooms
                highlighted = before[self.selected].id if self.room_picker and 0 <= self.selected < len(before) else old_id
                self.rooms = result
                visible = self.visible_rooms
                if self.room_picker:
                    self.selected = next((i for i, r in enumerate(visible) if r.id == highlighted), 0)
                elif old_id and any(r.id == old_id for r in visible):
                    self.selected = next(i for i, r in enumerate(visible) if r.id == old_id)
                elif visible:
                    self.selected = 0
                    self.choose(visible[0])
                else:
                    self.room_id, self.messages = None, []
                    self.draft, self.cursor = "", 0
                    self.note("지정한 이름의 방이 없습니다. Ctrl+O로 전체 방을 검색해 선택하세요.")
                if self.error and self.error_kind == "rooms":
                    self.note("연결됨")
            elif kind == "messages":
                self.messages = result
                self.last_refresh = datetime.now().strftime("%H:%M:%S")
                if not self.error and (self.mode == "browse" or self.status in ("불러오는 중…", "연결 중…")):
                    self.note("연결됨 · {}개 행".format(len(result)))
            elif kind == "send":
                self.sending_drafts.pop(context, None)
                self.drafts[context] = ""
                if context == self.room_id:
                    self.draft, self.cursor = "", 0
                    self.mode = "compose"
                    self.last_poll = float("-inf")
                self.note("전송 요청 완료 · 실제 반영은 대화 조회에서 확인합니다")
            elif kind == "open_room":
                self.mode = "compose"
                self.note("연결됨")
            self.dirty = True

    def poll(self):
        if self.cover:
            return
        now = time.monotonic()
        if now - self.last_rooms >= 15 and "rooms" not in self.pending:
            self.last_rooms = now
            self.job("rooms", None, self.backend.rooms)
        if self.room_id and now - self.last_poll >= self.args.interval and "messages" not in self.pending:
            self.last_poll = now
            room_id = self.room_id
            self.job("messages", room_id, lambda: self.backend.messages(room_id))

    def run(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        self.screen.timeout(100)
        # Control shortcuts must reach the app rather than terminal flow control.
        curses.raw()
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
        try:
            while self.running:
                self.collect()
                self.poll()
                if self.dirty:
                    self.draw()
                    self.dirty = False
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                self.handle(key)
                self.dirty = True
        finally:
            # An explicit send already in progress is never duplicated or interrupted halfway.
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.screen.erase()
            self.screen.refresh()
            try:
                curses.curs_set(1)
            except curses.error:
                pass

    def handle(self, key):
        if key in ("\x03", "\x11"):  # Ctrl+C / Ctrl+Q
            if "send" in self.pending or "open_room" in self.pending:
                self.note("카톡 작업 처리 중입니다. 완료 후 종료할 수 있습니다. Ctrl+B로 화면을 가릴 수 있습니다.")
            else:
                self.running = False
            return
        if key == curses.KEY_RESIZE:
            return
        if key == "\x02":  # Ctrl+B: available in every mode, including sending
            self.cover = not self.cover
            self.help = False
            return
        if self.cover:
            return
        if key == "\x12":
            self.last_poll, self.last_rooms = float("-inf"), float("-inf")
            self.note("새로고침 요청")
            return
        if key == "\x14":
            modes = ["console", "log", "sheet"]
            self.view = modes[(modes.index(self.view) + 1) % len(modes)]
            self.room_picker = False
            return
        if self.help:
            self.help = False
            return
        if self.mode in ("sending", "opening"):
            return
        if key == "\x0f" and self.view == "console":
            self.room_picker = not self.room_picker
            self.filter = ""
            rooms = self.visible_rooms
            self.selected = next((i for i, room in enumerate(rooms) if room.id == self.room_id), 0)
            self.mode = "browse" if self.room_picker else ("browse" if self.args.readonly else "compose")
            self.focus = "rooms" if self.room_picker else "messages"
            return
        if self.room_picker and self.view == "console":
            rooms = self.visible_rooms
            if key == "\x1b":
                self.room_picker = False
                self.filter = ""
                self.mode = "browse" if self.args.readonly else "compose"
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                delta = -1 if key == curses.KEY_UP else 1
                self.selected = max(0, min(len(rooms) - 1, self.selected + delta))
            elif key in ("\n", "\r", curses.KEY_ENTER) and rooms:
                self.choose(rooms[self.selected])
                self.room_picker = False
                self.filter = ""
                self.mode = "browse" if self.args.readonly else "compose"
                self.focus = "messages"
                if "open_room" in self.pending:
                    self.mode = "opening"
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.filter = self.filter[:-1]
                self.selected = 0
            elif key == "\x15":
                self.filter = ""
                self.selected = 0
            elif isinstance(key, str) and key.isprintable():
                self.filter += key
                self.selected = 0
            return
        if key == curses.KEY_PPAGE:
            self.offset += max(1, self.screen.getmaxyx()[0] - 12)
            return
        if key == curses.KEY_NPAGE:
            self.offset = max(0, self.offset - max(1, self.screen.getmaxyx()[0] - 12))
            return
        if self.mode == "search":
            if key in ("\n", "\r", curses.KEY_ENTER, "\x1b"):
                self.mode = "browse"
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.filter = self.filter[:-1]
            elif key == "\x15":
                self.filter = ""
            elif isinstance(key, str) and key.isprintable():
                self.filter += key
            rooms = self.visible_rooms
            self.selected = 0
            if rooms:
                self.choose(rooms[0])
                if key not in ("\n", "\r", curses.KEY_ENTER, "\x1b"):
                    self.mode = "search"
            return
        if self.mode == "compose":
            if key == "\x1b":
                self.mode = "browse"
            elif key == curses.KEY_UP:
                self.offset += 1
            elif key == curses.KEY_DOWN:
                self.offset = max(0, self.offset - 1)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if self.room and self.draft.strip():
                    room, text = self.room, self.draft
                    def deliver():
                        self.backend.prepare(room, text)
                        self.backend.send(room, text)
                    self.sending_drafts[room.id] = text
                    self.draft, self.cursor = "", 0
                    self.drafts[room.id] = ""
                    self.job("send", room.id, deliver)
                    self.mode = "sending"
                    self.note("전송 중…")
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b") and self.cursor:
                self.draft = self.draft[:self.cursor - 1] + self.draft[self.cursor:]
                self.cursor -= 1
            elif key == curses.KEY_DC:
                self.draft = self.draft[:self.cursor] + self.draft[self.cursor + 1:]
            elif key in (curses.KEY_LEFT, "\x02"):
                self.cursor = max(0, self.cursor - 1)
            elif key == curses.KEY_RIGHT:
                self.cursor = min(len(self.draft), self.cursor + 1)
            elif key in (curses.KEY_HOME, "\x01"):
                self.cursor = 0
            elif key in (curses.KEY_END, "\x05"):
                self.cursor = len(self.draft)
            elif key == "\x15":
                self.draft, self.cursor = "", 0
            elif isinstance(key, str) and key.isprintable() and len(self.draft) < 4000:
                self.draft = self.draft[:self.cursor] + key + self.draft[self.cursor:]
                self.cursor += len(key)
            return
        if key in ("q", "Q"):
            self.running = False
        elif key == "?":
            self.help = True
        elif key == "/":
            self.mode = "search"
        elif key in ("i", "\n", "\r", curses.KEY_ENTER):
            if self.room:
                if self.args.readonly:
                    self.note("읽기 전용 모드입니다.")
                else:
                    self.room_picker = False
                    self.mode = "compose"
                    self.note("입력 중 · Enter 전송 · Esc 목록")
        elif key == "\t":
            self.focus = "messages" if self.focus == "rooms" else "rooms"
        elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j"):
            delta = -1 if key in (curses.KEY_UP, "k") else 1
            if self.focus == "rooms":
                rooms = self.visible_rooms
                self.selected = max(0, min(len(rooms) - 1, self.selected + delta))
                if rooms:
                    self.choose(rooms[self.selected])
            else:
                self.offset = max(0, self.offset - delta)
        elif key == curses.KEY_PPAGE:
            self.offset += max(1, self.screen.getmaxyx()[0] - 12)
        elif key == curses.KEY_NPAGE:
            self.offset = max(0, self.offset - max(1, self.screen.getmaxyx()[0] - 12))
        elif key in (curses.KEY_END, "G"):
            self.offset = 0

    def put(self, y, x, text, attr=0, columns=None):
        height, cols = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= cols:
            return
        room = max(0, cols - x - 1)
        if columns is not None:
            room = min(room, columns)
        try:
            self.screen.addstr(y, x, clip(text, room), attr)
        except curses.error:
            pass

    def color(self, number):
        return curses.color_pair(number) if curses.has_colors() else 0

    def draw(self):
        self.screen.erase()
        height, cols = self.screen.getmaxyx()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if self.cover:
            self.draw_cover(height, cols)
        elif height < 20 or cols < 76:
            self.put(1, 2, "창을 76열 × 20행 이상으로 키워주세요.")
            self.put(3, 2, "Ctrl+B 가리기/복귀 · Ctrl+Q 종료")
        else:
            if self.view == "console":
                self.draw_console(height, cols)
            else:
                self.draw_main(height, cols)
            if self.help:
                self.draw_help(height, cols)
            elif self.room_picker and self.view == "console":
                self.draw_rooms(height, cols)
        self.screen.refresh()

    def draw_cover(self, height, cols):
        if self.view == "console":
            self.put(1, 2, ">_ Console", curses.A_BOLD)
            self.put(height - 4, 2, "─" * max(0, cols - 5), curses.A_DIM)
            self.put(height - 3, 2, "› ")
            self.put(height - 2, 2, "Ready", curses.A_DIM)
            return
        self.put(0, 1, "Workbook  /  Sheet1", curses.A_BOLD)
        self.put(2, 1, "    A                 B                 C                 D")
        for y in range(3, max(3, height - 2)):
            self.put(y, 1, "{:>3} │".format(y - 2))
            for x in range(24, cols - 1, 20):
                self.put(y, x, "│", curses.A_DIM)
        self.put(height - 1, 1, "Ready", curses.A_DIM)

    def draw_console(self, height, cols):
        """Codex-inspired layout: small session card, full-width transcript, prompt."""
        card_width = min(64, cols - 5)
        name = self.room.name if self.room else "연결 중"
        header = [
            "╭" + "─" * (card_width - 2) + "╮",
            ("│ >_ Sheetdesk {}  (local console)".format(__version__)).ljust(card_width - 1) + "│",
            "│ session: " + name,
            "╰" + "─" * (card_width - 2) + "╯",
        ]
        for i, line in enumerate(header):
            if i == 2:
                inside = clip(line, card_width - 1)
                line = inside + " " * max(0, card_width - 1 - width(inside)) + "│"
            self.put(i + 1, 2, line, curses.A_DIM if i in (0, 2, 3) else curses.A_BOLD, card_width)
        lines = []
        for msg in self.messages:
            lines.append(("  {}  {}".format(msg.sender, msg.clock), curses.A_DIM))
            for index, part in enumerate(wrap(msg.text, cols - 9)):
                prefix = ("› " if msg.mine else "• ") if index == 0 else "  "
                lines.append((prefix + part, curses.A_BOLD if msg.mine else 0))
            lines.append(("", 0))
        capacity = height - 13
        self.offset = min(self.offset, max(0, len(lines) - capacity))
        end = len(lines) - self.offset
        for index, (text, attr) in enumerate(lines[max(0, end - capacity):end]):
            self.put(6 + index, 2, text, attr, cols - 6)
        if not lines:
            self.put(7, 2, "연결된 대화를 기다리는 중…", curses.A_DIM)
        self.put(height - 6, 2, self.status, self.color(3) if self.error else curses.A_DIM, cols - 6)
        self.put(height - 5, 2, "─" * (cols - 5), curses.A_DIM)
        draft = self.filter if self.mode == "search" else self.draft
        cursor = len(draft) if self.mode == "search" else self.cursor
        begin = 0
        while width(draft[begin:cursor]) > cols - 9:
            begin += 1
        self.put(height - 4, 2, ("/ " if self.mode == "search" else "› ") + draft[begin:], 0, cols - 6)
        self.put(height - 3, 2, "─" * (cols - 5), curses.A_DIM)
        footer = "Enter 전송 · Ctrl+O 목록 · Ctrl+B 가리기 · Ctrl+Q 종료"
        self.put(height - 2, 2, footer, curses.A_DIM, cols - 6)
        mode = "읽기 전용" if self.args.readonly else "입력 중" if self.mode == "compose" else "탐색"
        indicator = "{} · {}s · {}".format(mode, self.args.interval, self.last_refresh or "연결 중")
        if self.args.demo:
            indicator += " · DEMO"
        if self.offset:
            indicator += " · 과거 대화 (↓ / PgDn으로 최신)"
        self.put(height - 1, 2, indicator, curses.A_DIM, cols - 6)
        if self.mode in ("compose", "search"):
            try:
                self.screen.move(height - 4, min(cols - 2, 4 + width(draft[begin:cursor])))
                curses.curs_set(1)
            except curses.error:
                pass

    def draw_rooms(self, height, cols):
        y, x, w, h = self.panel(height, cols, "전체 대화방 목록 · 검색: " + self.filter)
        rooms = self.visible_rooms
        capacity = h - 4
        start = max(0, self.selected - capacity + 1)
        for line, room in enumerate(rooms[start:start + capacity]):
            index = start + line
            self.put(y + 2 + line, x + 2, ("› " if index == self.selected else "  ") + room.name,
                     curses.A_REVERSE | (curses.A_BOLD if index == self.selected else 0), w - 4)
        if not rooms:
            self.put(y + 2, x + 2, "일치하는 대화방이 없습니다.", curses.A_REVERSE, w - 4)
        self.put(y + h - 2, x + 2, "이름 입력 검색 · ↑↓ 이동 · Enter 선택 · Esc 닫기", curses.A_REVERSE, w - 4)

    def draw_main(self, height, cols):
        left = 27 if cols >= 105 else 23
        body_x = left + 2
        body_width = cols - body_x - 2
        title = "Workbook — Sheetdesk" + ("  [DEMO]" if self.args.demo else "")
        self.put(0, 1, title, curses.A_BOLD | self.color(1))
        self.put(1, 1, "File   Home   View     Ctrl+B 가리기   Ctrl+T 표/로그   ? 도움말", curses.A_DIM)
        self.put(2, 0, "─" * (cols - 1), curses.A_DIM)
        room_label = "시트 찾기: " + self.filter if self.mode == "search" else "시트 목록" + (" / " + self.filter if self.filter else "")
        self.put(3, 1, room_label, curses.A_BOLD, left - 1)
        rooms = self.visible_rooms
        capacity = height - 10
        start = max(0, self.selected - capacity + 1)
        for line, room in enumerate(rooms[start:start + capacity]):
            index = start + line
            marker = "› " if room.id == self.room_id else "  "
            attr = curses.A_REVERSE if index == self.selected and self.focus == "rooms" else 0
            suffix = " ·{}".format(room.unread) if room.unread else ""
            self.put(4 + line, 1, marker + room.name + suffix, attr, left - 1)
        for y in range(3, height - 6):
            self.put(y, left, "│", curses.A_DIM)
        name = self.room.name if self.room else "선택된 시트 없음"
        self.put(3, body_x, name, curses.A_BOLD | self.color(2), body_width)
        if self.view == "sheet":
            self.put(4, body_x, "    │ A · 일시     │ B · 작성자      │ C · 내용", curses.A_DIM, body_width)
        else:
            self.put(4, body_x, "TIMESTAMP     AUTHOR           TEXT", curses.A_DIM, body_width)
        lines = []
        # Wrap each message, retaining all of its content for vertical scrolling.
        content_width = max(4, body_width - 39)
        for index, msg in enumerate(self.messages):
            for part_index, part in enumerate(wrap(msg.text, content_width)):
                clock = msg.clock if part_index == 0 else ""
                sender = clip(msg.sender, 15) if part_index == 0 else ""
                sender = sender + " " * max(0, 15 - width(sender))
                if self.view == "sheet":
                    row = "{:>3} │ {:12} │ {} │ {}".format(index + 1 if part_index == 0 else "", clock, sender, part)
                else:
                    row = "{:12}  {}  {}".format(clock, sender, part)
                lines.append((row, msg.mine))
        rows = height - 12
        self.offset = min(self.offset, max(0, len(lines) - rows))
        end = len(lines) - self.offset
        visible = lines[max(0, end - rows):end]
        for i, (line, mine) in enumerate(visible):
            self.put(5 + i, body_x, line, self.color(2) if mine else 0, body_width)
        if not self.messages:
            self.put(6, body_x, "대화가 없거나 아직 불러오는 중입니다.", curses.A_DIM, body_width)
        self.put(height - 6, 0, "─" * (cols - 1), curses.A_DIM)
        status = ("! " if self.error else "● ") + self.status
        self.put(height - 5, 1, status, self.color(3) if self.error else curses.A_DIM)
        indicator = "조회 {} · {}초 간격".format(self.last_refresh or "—", self.args.interval)
        if self.offset:
            indicator += " · 과거 행 보는 중 (G 최신)"
        self.put(height - 4, 1, indicator, curses.A_DIM)
        prefix = "찾기 > " if self.mode == "search" else "fx > "
        draft = self.filter if self.mode == "search" else self.draft
        cursor = len(draft) if self.mode == "search" else self.cursor
        # Horizontal viewport follows the editing cursor, preserving Korean character widths.
        begin = 0
        while width(draft[begin:cursor]) > cols - width(prefix) - 5:
            begin += 1
        self.put(height - 3, 1, prefix + draft[begin:])
        self.put(height - 2, 1, "i 입력 · Enter 전송 · Tab 영역 · / 방 검색 · PgUp/PgDn", curses.A_DIM)
        self.put(height - 1, 1, "Ctrl+B 가리기/복귀 · Ctrl+R 새로고침 · Ctrl+Q 종료" + (" · READ ONLY" if self.args.readonly else ""), curses.A_DIM)
        if self.mode in ("compose", "search"):
            try:
                self.screen.move(height - 3, min(cols - 2, 1 + width(prefix) + width(draft[begin:cursor])))
                curses.curs_set(1)
            except curses.error:
                pass

    def panel(self, height, cols, title):
        panel_width = min(86, cols - 6)
        panel_height = min(14, height - 4)
        y, x = (height - panel_height) // 2, (cols - panel_width) // 2
        for line in range(panel_height):
            self.put(y + line, x, " " * panel_width, curses.A_REVERSE, panel_width)
        self.put(y, x + 2, title, curses.A_BOLD | curses.A_REVERSE, panel_width - 4)
        return y, x, panel_width, panel_height

    def draw_help(self, height, cols):
        y, x, w, h = self.panel(height, cols, "키보드 사용법")
        tips = ["↑↓ / j k : 방 이동 · Tab : 방/대화 영역 전환",
                "PgUp/PgDn : 대화 스크롤 · G : 최신 메시지",
                "i / Enter : 입력 · ←→ Home End : 커서 이동",
                "입력 중 Enter : 바로 전송 (대상·창 상태 검사는 유지)",
                "Esc : 입력 모드 종료 · Ctrl+U : 입력 지우기",
                "Ctrl+O : 콘솔 방 목록 · Ctrl+R : 갱신 · Ctrl+T : 화면",
                "Ctrl+B : 즉시 빈 표로 전환/복귀 (전송 중에도 가능)",
                "가린 동안 폴링 일시정지 · Ctrl+Q : 종료",
                "별도 저장/외부 전송 없음 · 현재 로컬 DB만 조회",
                "아무 키나 누르면 닫힘"]
        for i, tip in enumerate(tips[:h - 2]):
            self.put(y + i + 2, x + 2, tip, curses.A_REVERSE, w - 4)

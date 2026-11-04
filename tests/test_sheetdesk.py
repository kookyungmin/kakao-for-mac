import argparse
import curses
import subprocess
import unittest
from unittest.mock import patch

from sheetdesk.core import (BackendError, DemoBackend, KakaoBackend, Message,
                            Room, clip, ordered_messages, safe_text, width, wrap)
from sheetdesk.ui import App


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = KakaoBackend("/fake/kakaocli")
        self.room = Room("9007199254740993", "친구")

    def response(self, out="", code=0):
        return subprocess.CompletedProcess([], code, out, "")

    def test_prepare_dry_run_and_body_uses_stdin_not_argv(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[1] == "chats":
                return self.response('[{"id":9007199254740993,"display_name":"친구"}]')
            if args[1] == "diagnostics":
                return self.response('{"background_ready":true,"hidden":true}')
            return self.response("DRY RUN")
        text = '-n $(touch /tmp/should-never-exist) "한글"'
        with patch("sheetdesk.core.subprocess.run", side_effect=run):
            self.backend.prepare(self.room, text)
        argv, kwargs = calls[-1]
        self.assertIn("--dry-run", argv)
        self.assertIn("--message-stdin", argv)
        self.assertNotIn(text, argv)
        self.assertEqual(kwargs["input"], text)
        self.assertFalse(kwargs["shell"])

    def test_send_requires_exact_patched_cli_and_passes_stdin(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[1] == "chats":
                return self.response('[{"id":9007199254740993,"display_name":"친구"}]')
            if "--help" in args:
                return self.response("--exact --message-stdin --background")
            return self.response("sent")
        with patch("sheetdesk.core.subprocess.run", side_effect=run):
            self.backend.send(self.room, "민감한 메시지")
        argv, kwargs = calls[-1]
        self.assertEqual(argv[1:], ["send", "--exact", "--background", "--message-stdin", "--", "친구"])
        self.assertEqual(kwargs["input"], "민감한 메시지")

    def test_duplicate_names_block_sending(self):
        with patch.object(self.backend, "rooms", return_value=[self.room, Room("2", "친구")]), \
             patch.object(self.backend, "run") as run:
            with self.assertRaises(BackendError):
                self.backend.send(self.room, "안녕")
            run.assert_not_called()

    def test_renamed_room_same_name_other_id_blocks(self):
        with patch.object(self.backend, "rooms", return_value=[Room("2", "친구")]):
            with self.assertRaises(BackendError):
                self.backend.validate_target(self.room)

    def test_readonly_never_executes_send(self):
        backend = KakaoBackend("/fake", readonly=True)
        with patch.object(backend, "run") as run:
            with self.assertRaises(BackendError):
                backend.send(self.room, "안녕")
            run.assert_not_called()

    def test_upstream_unpatched_binary_cannot_send(self):
        with patch.object(self.backend, "rooms", return_value=[self.room]), \
             patch.object(self.backend, "run", return_value="--dry-run") as run:
            with self.assertRaises(BackendError):
                self.backend.send(self.room, "안녕")
            run.assert_called_once_with(["send", "--help"])

    def test_prepare_does_not_hide_or_require_background_focus(self):
        with patch.object(self.backend, "rooms", return_value=[self.room]), \
             patch.object(self.backend, "diagnostics") as diagnostics, \
             patch.object(self.backend, "run") as run:
            self.backend.prepare(self.room, "안녕")
            diagnostics.assert_not_called()
            run.assert_called_once_with(
                ["send", "--dry-run", "--exact", "--background", "--message-stdin", "--", self.room.name],
                private=True, input_text="안녕")

    def test_send_without_background_capability_is_blocked(self):
        with patch.object(self.backend, "rooms", return_value=[self.room]), \
             patch.object(self.backend, "run", return_value="--exact --message-stdin") as run:
            with self.assertRaises(BackendError):
                self.backend.send(self.room, "안녕")
            run.assert_called_once_with(["send", "--help"])

    def test_foreground_only_cli_is_blocked(self):
        with patch.object(self.backend, "rooms", return_value=[self.room]), \
             patch.object(self.backend, "run", return_value="--exact --message-stdin --foreground") as run:
            with self.assertRaises(BackendError):
                self.backend.send(self.room, "안녕")
            run.assert_called_once_with(["send", "--help"])

    def test_two_second_rate_limit(self):
        self.backend._last_send = 100
        with patch("sheetdesk.core.time.monotonic", return_value=101), \
             patch.object(self.backend, "run") as run:
            with self.assertRaises(BackendError):
                self.backend.send(self.room, "안녕")
            run.assert_not_called()

    def test_window_report_is_explicit_and_passes_selected_room(self):
        with patch.object(self.backend, "run", return_value='{"windows":[]}') as run:
            self.assertEqual(self.backend.diagnostics("경민", windows=True), {"windows": []})
            run.assert_called_once_with(["diagnostics", "--windows", "--chat", "경민"])

    def test_focus_room_validates_target_and_never_sends_message(self):
        with patch.object(self.backend, "rooms", return_value=[self.room]), \
             patch.object(self.backend, "run", return_value='{"chat_prepared":true}') as run:
            self.backend.focus_room(self.room)
            run.assert_called_once_with(
                ["diagnostics", "--open-chat", "--chat", self.room.name], private=True)

    def test_duplicate_room_cannot_be_automatically_opened(self):
        with patch.object(self.backend, "rooms", return_value=[self.room, Room("2", self.room.name)]), \
             patch.object(self.backend, "run") as run:
            with self.assertRaises(BackendError):
                self.backend.focus_room(self.room)
            run.assert_not_called()

    def test_private_error_does_not_echo_message(self):
        bad = self.response("secret-message", code=1)
        with patch("sheetdesk.core.subprocess.run", return_value=bad):
            with self.assertRaises(BackendError) as error:
                self.backend.run(["send"], private=True)
        self.assertNotIn("secret-message", str(error.exception))

    def test_accessibility_failure_has_specific_action_without_body(self):
        bad = self.response("Failed to send message: accessibility_permission_denied secret-body", code=1)
        with patch("sheetdesk.core.subprocess.run", return_value=bad):
            with self.assertRaises(BackendError) as error:
                self.backend.run(["send"], private=True)
        self.assertIn("손쉬운 사용", str(error.exception))
        self.assertNotIn("secret-body", str(error.exception))

    def test_timeout_is_uncertain_not_auto_retry(self):
        with patch("sheetdesk.core.subprocess.run", side_effect=subprocess.TimeoutExpired(["send"], 40)) as run:
            with self.assertRaisesRegex(BackendError, "전송 결과"):
                self.backend.run(["send"], private=True)
            self.assertEqual(run.call_count, 1)

    def test_messages_sorted_deduplicated_and_64_bit_ids_preserved(self):
        a = {"id": 9007199254740993, "text": "먼저", "timestamp": "2026-11-04T00:00:00Z"}
        b = {"id": 9007199254740994, "text": "다음", "timestamp": "2026-11-04T00:00:00Z"}
        rows = ordered_messages([b, a, b])
        self.assertEqual([m.text for m in rows], ["먼저", "다음"])
        self.assertEqual(rows[0].id, "9007199254740993")

    def test_terminal_controls_removed_and_korean_width_preserved(self):
        self.assertNotIn("\x1b", safe_text("hello\x1b[2J"))
        self.assertNotIn("\u202e", safe_text("hello\u202e"))
        self.assertEqual(width("가나다"), 6)
        self.assertEqual(clip("가나다", 5), "가나")
        text = "한글 대화 abc xyz 긴 메시지"
        lines = wrap(text, 8)
        self.assertEqual("".join(lines), text)
        self.assertTrue(all(width(line) <= 8 for line in lines))


class FakeScreen:
    def __init__(self):
        self.cells = []
    def getmaxyx(self):
        return 32, 120
    def erase(self):
        self.cells = []
    def addstr(self, y, x, text, attr=0):
        self.cells.append((y, x, text))
    def refresh(self):
        pass
    def move(self, *args):
        pass


class UITests(unittest.TestCase):
    def setUp(self):
        self.app = App(FakeScreen(), DemoBackend(), argparse.Namespace(
            demo=True, readonly=False, interval=2, view="sheet"))
        self.app.rooms = [Room("1", "비밀 이름"), Room("2", "다른 방")]
        self.app.choose(self.app.rooms[0])

    def tearDown(self):
        self.app.pool.shutdown(wait=True)

    def test_cover_has_no_sensitive_text_and_does_not_poll(self):
        self.app.messages = [Message("1", "", "비밀 이름", "민감한 대화")]
        self.app.draft = "민감한 초안"
        self.app.status = "민감한 상태"
        self.app.handle("\x02")
        with patch("sheetdesk.ui.curses.curs_set"), patch.object(self.app, "job") as job:
            self.app.draw()
            self.app.poll()
            job.assert_not_called()
        rendered = "".join(c[2] for c in self.app.screen.cells)
        self.assertNotIn("비밀", rendered)
        self.assertNotIn("민감", rendered)
        self.assertIn("Workbook", rendered)

    def test_old_room_poll_does_not_replace_new_room(self):
        self.app.choose(self.app.rooms[1])
        self.app.events.put(("messages", "1", [Message("1", "", "A", "stale")], None))
        self.app.collect()
        self.assertEqual(self.app.messages, [])

    def test_enter_prepares_and_sends_without_confirmation(self):
        self.app.mode, self.app.draft = "compose", "초안"
        with patch.object(self.app, "job") as job:
            self.app.handle("\n")
            self.assertEqual(job.call_args[0][0], "send")
            deliver = job.call_args[0][2]
            with patch.object(self.app.backend, "prepare") as prepare, patch.object(self.app.backend, "send") as send:
                deliver()
                prepare.assert_called_once_with(self.app.room, "초안")
                send.assert_called_once_with(self.app.room, "초안")
            self.assertEqual(self.app.mode, "sending")
            self.assertEqual(self.app.draft, "")
            self.assertEqual(self.app.cursor, 0)
            self.app.handle("\n")
            self.assertEqual(job.call_count, 1)

    def test_failed_send_restores_unsent_draft(self):
        self.app.mode, self.app.draft = "compose", "보낼 내용"
        with patch.object(self.app, "job"):
            self.app.handle("\n")
        self.app.events.put(("send", self.app.room_id, {"delivery_uncertain": False}, "입력 실패"))
        self.app.collect()
        self.assertEqual(self.app.draft, "보낼 내용")
        self.assertEqual(self.app.cursor, len("보낼 내용"))

    def test_uncertain_send_leaves_input_empty_without_status_warning(self):
        self.app.mode, self.app.draft = "compose", "이미 보냈을 수 있는 내용"
        with patch.object(self.app, "job"):
            self.app.handle("\n")
        self.app.events.put(("send", self.app.room_id, {"delivery_uncertain": True}, "전송 결과 미확인"))
        self.app.collect()
        self.assertEqual(self.app.draft, "")
        self.app.events.put(("rooms", None, list(self.app.rooms), None))
        self.app.events.put(("messages", self.app.room_id, [], None))
        self.app.collect()
        self.assertFalse(self.app.error)
        self.assertEqual(self.app.status, "")

    def test_enter_with_empty_draft_does_not_send(self):
        self.app.mode, self.app.draft = "compose", "   "
        with patch.object(self.app, "job") as job:
            self.app.handle("\n")
            job.assert_not_called()

    def test_korean_input_and_draft_per_room(self):
        self.app.mode = "compose"
        for key in "안녕하세요":
            self.app.handle(key)
        self.app.choose(self.app.rooms[1])
        self.assertEqual(self.app.draft, "")
        self.app.choose(self.app.rooms[0])
        self.assertEqual(self.app.draft, "안녕하세요")

    def test_demo_pingpong_without_real_backend(self):
        room = self.app.backend.rooms()[0]
        self.app.backend.send(room, "샘플")
        rows = self.app.backend.messages(room.id)
        self.assertEqual(rows[-2].text, "샘플")
        self.assertEqual(rows[-1].text, "샘플 답장: 샘플")


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.app = App(FakeScreen(), DemoBackend(), argparse.Namespace(
            demo=True, readonly=False, interval=2, view="console", rooms=["김도완"]))
        self.room = Room("1", "김도완")

    def tearDown(self):
        self.app.pool.shutdown(wait=True)

    def test_refresh_only_retains_exact_allowed_room(self):
        rooms = [Room("2", "김도완 회사"), Room("3", "홍길동"), self.room]
        self.app.events.put(("rooms", None, rooms, None))
        self.app.collect()
        self.assertEqual(self.app.visible_rooms, [self.room])
        self.assertEqual(self.app.room_id, "1")
        self.assertEqual(self.app.mode, "compose")
        self.app.filter = "홍길동"
        self.assertEqual(self.app.visible_rooms, [])

    def test_unmatched_name_never_selects_another_room(self):
        self.app.events.put(("rooms", None, [Room("2", "다른 사람")], None))
        self.app.collect()
        self.assertIsNone(self.app.room_id)

    def test_picker_lists_all_rooms_and_switches_only_on_enter(self):
        other = Room("2", "다른 친구")
        self.app.events.put(("rooms", None, [self.room, other], None))
        self.app.collect()
        self.app.handle("\x0f")
        self.assertEqual(self.app.visible_rooms, [self.room, other])
        self.app.handle(curses.KEY_DOWN)
        self.assertEqual(self.app.room_id, "1")
        self.app.handle("\n")
        self.assertEqual(self.app.room_id, "2")
        self.assertFalse(self.app.room_picker)
        self.assertEqual(self.app.visible_rooms, [other])

    def test_picker_search_and_cancel_keep_current_room(self):
        other = Room("2", "다른 친구")
        self.app.events.put(("rooms", None, [self.room, other], None))
        self.app.collect()
        self.app.handle("\x0f")
        for key in "친구":
            self.app.handle(key)
        self.assertEqual(self.app.visible_rooms, [other])
        self.app.handle("\x1b")
        self.assertEqual(self.app.room_id, "1")
        self.assertEqual(self.app.visible_rooms, [self.room])

    def test_picker_switch_refresh_poll_and_send_use_new_room(self):
        other = Room("2", "다른 친구")
        rooms = [self.room, other]
        self.app.events.put(("rooms", None, rooms, None))
        self.app.collect()
        self.app.draft = "원래 방 초안"
        self.app.handle("\x0f")
        for key in "친구":
            self.app.handle(key)
        self.app.handle("\n")
        self.assertEqual(self.app.room, other)
        self.assertEqual(self.app.draft, "")
        self.app.events.put(("rooms", None, list(reversed(rooms)), None))
        self.app.events.put(("messages", self.room.id, [Message("1", "", "", "이전 방 응답")], None))
        self.app.collect()
        self.assertEqual(self.app.room, other)
        self.assertEqual(self.app.messages, [])
        with patch.object(self.app, "job") as job, patch.object(self.app.backend, "messages") as messages:
            self.app.poll()
            request = next(call for call in job.call_args_list if call.args[0] == "messages")
            self.assertEqual(request.args[1], other.id)
            request.args[2]()
            messages.assert_called_once_with(other.id)
        self.app.draft = "새 방 답장"
        with patch.object(self.app, "job") as job, \
             patch.object(self.app.backend, "prepare") as prepare, \
             patch.object(self.app.backend, "send") as send:
            self.app.handle("\n")
            job.call_args.args[2]()
            prepare.assert_called_once_with(other, "새 방 답장")
            send.assert_called_once_with(other, "새 방 답장")
        self.app.events.put(("send", other.id, None, None))
        self.app.collect()
        self.app.handle("\x0f")
        for key in "김도완":
            self.app.handle(key)
        self.app.handle("\n")
        self.assertEqual(self.app.room, self.room)
        self.assertEqual(self.app.draft, "원래 방 초안")

    def test_missing_initial_room_can_be_recovered_with_picker(self):
        other = Room("2", "다른 친구")
        self.app.events.put(("rooms", None, [other], None))
        self.app.collect()
        self.assertIsNone(self.app.room)
        self.app.handle("\x0f")
        self.app.handle("\n")
        self.assertEqual(self.app.room, other)
        self.assertEqual(self.app.mode, "compose")

    def test_live_picker_prepares_selected_window_before_allowing_send(self):
        other = Room("2", "다른 친구")
        self.app.events.put(("rooms", None, [self.room, other], None))
        self.app.collect()
        self.app.args.demo = False
        self.app.handle("\x0f")
        self.app.handle(curses.KEY_DOWN)
        requests = []
        def job(kind, context, fn):
            self.app.pending.add(kind)
            requests.append((kind, context, fn))
        with patch.object(self.app, "job", side_effect=job), \
             patch.object(self.app.backend, "focus_room", create=True) as focus:
            self.app.handle("\n")
            self.assertEqual(self.app.mode, "opening")
            self.assertEqual(requests[0][:2], ("open_room", other.id))
            requests[0][2]()
            focus.assert_called_once_with(other)
            self.app.handle("\n")
            self.assertEqual(len(requests), 1)
        self.app.events.put(("open_room", other.id, None, None))
        self.app.collect()
        self.assertEqual(self.app.mode, "compose")
        self.assertEqual(self.app.room, other)

    def test_spacing_variant_is_allowed_but_keeps_actual_send_name(self):
        actual = Room("9", "김 도완")
        self.app.events.put(("rooms", None, [actual, Room("3", "김 도완 회사")], None))
        self.app.collect()
        self.assertEqual(self.app.visible_rooms, [actual])
        self.assertEqual(self.app.room.name, "김 도완")
        self.app.choose(Room("2", "다른 사람"))
        self.assertEqual(self.app.room_id, "9")

    def test_console_has_prompt_and_no_spreadsheet(self):
        self.app.rooms = [self.room]
        self.app.choose(self.room)
        self.app.messages = [Message("1", "", "김도완", "샘플 답장")]
        with patch("sheetdesk.ui.curses.curs_set"), patch("sheetdesk.ui.curses.has_colors", return_value=False):
            self.app.draw()
        rendered = "".join(c[2] for c in self.app.screen.cells)
        self.assertIn(">_ Sheetdesk", rendered)
        self.assertIn("› ", rendered)
        self.assertIn("샘플 답장", rendered)
        self.assertNotIn("Workbook", rendered)
        self.assertNotIn("fx >", rendered)

    def test_console_cover_removes_name_and_draft(self):
        self.app.rooms = [self.room]
        self.app.choose(self.room)
        self.app.draft = "민감한 내용"
        self.app.handle("\x02")
        with patch("sheetdesk.ui.curses.curs_set"):
            self.app.draw()
        rendered = "".join(c[2] for c in self.app.screen.cells)
        self.assertNotIn("김도완", rendered)
        self.assertNotIn("민감한 내용", rendered)
        self.assertNotIn("Workbook", rendered)


if __name__ == "__main__":
    unittest.main()

"""Headless presentation/consent tests; no native Tk render claim."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from svn_client.fileops import FileOperationPlan
from svn_client.fileops_dialogs import (FileDestinationDialog, FileOperationConfirmDialog,
                                       confirmation_text, display_path, validate_destination)


def plan(operation="move", paths=("src/old.txt",), destination="src/new.txt"):
    if operation == "delete":
        destination = None
    affected = paths + ((destination,) if destination is not None else ())
    return FileOperationPlan(operation, "/work/副本", paths, destination, affected, (), (), "a" * 64,
                             ("Do not edit concurrently.", "No repository commit is performed."))


class FileOperationDialogPresentationTests(unittest.TestCase):
    def test_destination_preserves_unicode_spaces_and_literal_at(self):
        self.assertEqual(validate_destination("目录/ new @file.txt", "a.txt"), "目录/ new @file.txt")

    def test_destination_normalizes_windows_separator_without_io(self):
        self.assertEqual(validate_destination("folder\\new.txt", "old.txt"), "folder/new.txt")

    def test_destination_rejects_ambiguous_absolute_and_traversal_paths(self):
        for value in ("", " ", "/tmp/new", "\\share\\new", "C:\\new", "C:new", "../new", "a/../b", "./b",
                      "a//b", "a/", "https://example.test/a", "file:///a", "a\x00b", "a\nb", ".svn/entries",
                      "folder/.SVN/x", "NUL", "con.txt", "a.txt:stream", "folder./a", "file.txt "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_destination(value, "old.txt")

    def test_destination_rejects_same_or_case_only_target(self):
        for value in ("folder/old.txt", "Folder/OLD.txt", "folder\\old.txt"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_destination(value, "folder\\old.txt")

    def test_path_display_quotes_controls_and_keeps_unicode(self):
        self.assertEqual(display_path("目录/line\nnext\tfile"), '"目录/line\\nnext\\tfile"')
        self.assertEqual(json.loads(display_path('a"b')), 'a"b')

    def test_full_multi_file_delete_scope_and_risks_visible_without_truncation(self):
        paths = tuple(f"folder/{index:03d}.txt" for index in range(128))
        value = plan("delete", paths)
        text = confirmation_text(value)
        for path in paths:
            self.assertEqual(text.count(display_path(path)), 2)
        for term in ("移除当前本地文件", "BASE", "独立备份", "不提交仓库", "不是事务", "不能盲目重试", "a" * 64):
            self.assertIn(term, text)
        for warning in value.warnings:
            self.assertIn(warning, text)
        self.assertIn(display_path(value.workspace), text)

    def test_move_and_copy_show_exact_source_destination_and_pending_commit(self):
        for operation in ("move", "copy"):
            text = confirmation_text(plan(operation))
            self.assertIn('"src/old.txt"', text)
            self.assertIn('"src/new.txt"', text)
            self.assertIn("本地内容/属性", text)
            self.assertIn("另行提交", text)
            self.assertIn("不能自动扩展", text)
        self.assertIn("一起审查移动两端", confirmation_text(plan("move")))
        self.assertIn("源文件保留", confirmation_text(plan("copy")))

    def test_malformed_plan_refused_before_consent(self):
        base = plan()
        invalid = (replace(base, operation="commit"), replace(base, paths=()), replace(base, destination=None),
                   replace(base, affected_paths=("another.txt",)), replace(base, state_sha256="bad"),
                   replace(base, workspace=""), replace(base, scope="directories"), replace(base, commit_required=False),
                   replace(base, paths=("a.txt", "a.txt")), replace(plan("delete"), destination="unexpected.txt"))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                confirmation_text(value)


class FileOperationDialogCallbackTests(unittest.TestCase):
    def destination(self, value):
        return SimpleNamespace(source="old.txt", destination_var=Mock(get=lambda: value),
                               next_button=Mock(), result=None, destroy=Mock())

    def test_destination_only_returns_valid_string_does_not_execute(self):
        dialog = self.destination("folder/new.txt")
        FileDestinationDialog._accept(dialog)
        self.assertEqual(dialog.result, "folder/new.txt")
        dialog.destroy.assert_called_once_with()

    def test_destination_invalid_input_does_not_close_or_set_result(self):
        dialog = self.destination("../new")
        with patch("svn_client.fileops_dialogs.messagebox.showwarning") as warning:
            FileDestinationDialog._accept(dialog)
        warning.assert_called_once()
        self.assertIsNone(dialog.result)
        dialog.destroy.assert_not_called()

    def test_destination_button_tracks_valid_string(self):
        for value, expected in (("", "disabled"), ("../new", "disabled"), ("folder/new", "normal")):
            dialog = self.destination(value)
            FileDestinationDialog._update_button(dialog)
            dialog.next_button.configure.assert_called_once_with(state=expected)

    def test_confirm_requires_explicit_manual_ack_for_every_operation(self):
        for operation in ("delete", "move", "copy"):
            dialog = SimpleNamespace(operation=operation, risk_var=Mock(get=lambda: False), confirmed=False, destroy=Mock())
            with patch("svn_client.fileops_dialogs.messagebox.showwarning") as warning:
                FileOperationConfirmDialog._confirm(dialog)
            warning.assert_called_once()
            self.assertFalse(dialog.confirmed)
            dialog.destroy.assert_not_called()

    def test_acknowledgment_then_confirm_only_sets_consent(self):
        dialog = SimpleNamespace(operation="delete", risk_var=Mock(get=lambda: True), confirmed=False, destroy=Mock())
        FileOperationConfirmDialog._confirm(dialog)
        self.assertTrue(dialog.confirmed)
        dialog.destroy.assert_called_once_with()

    def test_confirm_button_disabled_until_ack(self):
        for checked, expected in ((False, "disabled"), (True, "normal")):
            dialog = SimpleNamespace(risk_var=Mock(get=lambda: checked), confirm_button=Mock())
            FileOperationConfirmDialog._update_confirm(dialog)
            dialog.confirm_button.configure.assert_called_once_with(state=expected)


class FileOperationDialogConstructionTests(unittest.TestCase):
    """Mock Tk construction to verify defaults/focus/bindings without a display."""
    def setUp(self):
        self.parent = Mock()
        self.parent.display.px.side_effect = lambda value: value
        self.parent.display.font.return_value = ("sans", 10)
        self.buttons = []
        self.variables = []
        self.patches = []
        def start(target, **kwargs):
            holder = patch(target, **kwargs)
            self.patches.append(holder)
            return holder.start()
        self.toplevel_init = start("svn_client.fileops_dialogs.tk.Toplevel.__init__", return_value=None)
        self.text = start("svn_client.fileops_dialogs._readonly_text")
        start("svn_client.fileops_dialogs._size")
        for name in ("title", "transient", "grab_set", "wait_window", "destroy"):
            start("svn_client.fileops_dialogs.tk.Toplevel." + name)
        self.bind = start("svn_client.fileops_dialogs.tk.Toplevel.bind")
        for name in ("Frame", "Label", "Entry", "Checkbutton"):
            start("svn_client.fileops_dialogs.ttk." + name)
        def button(*args, **kwargs):
            obj = Mock()
            self.buttons.append((kwargs, obj))
            return obj
        start("svn_client.fileops_dialogs.ttk.Button", side_effect=button)
        def variable(*args, **kwargs):
            obj = Mock()
            obj.get.return_value = kwargs.get("value")
            self.variables.append((kwargs, obj))
            return obj
        start("svn_client.fileops_dialogs.tk.StringVar", side_effect=variable)
        start("svn_client.fileops_dialogs.tk.BooleanVar", side_effect=variable)
        self.addCleanup(lambda: [holder.stop() for holder in reversed(self.patches)])

    def test_destination_initially_empty_cancel_focused_and_no_enter_execution(self):
        dialog = FileDestinationDialog(self.parent, "move", "old.txt", "/work")
        self.assertIsNone(dialog.result)
        self.assertEqual(self.variables[0][0]["value"], "")
        next_button = next(kwargs for kwargs, _ in self.buttons if kwargs["text"] == "继续预检…")
        self.assertEqual(next_button["state"], "disabled")
        cancel = next(obj for kwargs, obj in self.buttons if kwargs["text"] == "取消")
        cancel.focus_set.assert_called_once_with()
        self.assertEqual([call.args[0] for call in self.bind.call_args_list], ["<Escape>"])

    def test_delete_initially_unconfirmed_unchecked_and_cancel_focused(self):
        value = plan("delete")
        dialog = FileOperationConfirmDialog(self.parent, value)
        self.assertFalse(dialog.confirmed)
        self.assertEqual(self.variables[0][0]["value"], False)
        confirmation = next(kwargs for kwargs, _ in self.buttons if kwargs["text"].startswith("确认"))
        self.assertEqual(confirmation["state"], "disabled")
        cancel = next(obj for kwargs, obj in self.buttons if kwargs["text"] == "取消")
        cancel.focus_set.assert_called_once_with()
        self.assertEqual([call.args[0] for call in self.bind.call_args_list], ["<Escape>"])
        self.assertEqual(self.text.call_args.args[2], confirmation_text(value))

    def test_invalid_scope_rejected_before_window_creation(self):
        with self.assertRaises(ValueError):
            FileOperationConfirmDialog(self.parent, replace(plan(), affected_paths=("other",)))
        self.toplevel_init.assert_not_called()


if __name__ == "__main__":
    unittest.main()

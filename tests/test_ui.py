"""UI-independent presentation and safety tests; no display server required."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import StatusEntry
from svn_client.ui import SvnDesk, local_files, read_preview, status_label


class PresentationTests(unittest.TestCase):
    def test_regular_status(self):
        self.assertEqual(status_label(StatusEntry("a", "modified")), "已修改")

    def test_property_change(self):
        self.assertEqual(status_label(StatusEntry("a", "normal", props="modified")), "属性修改")

    def test_property_conflict(self):
        self.assertEqual(status_label(StatusEntry("a", "normal", props="conflicted")), "属性冲突")

    def test_tree_conflict(self):
        self.assertEqual(status_label(StatusEntry("a", "normal", tree_conflicted=True)), "树冲突")

    def test_unknown_status_is_not_hidden(self):
        self.assertEqual(status_label(StatusEntry("a", "future-state")), "future-state")

    def test_sizes(self):
        self.assertEqual(SvnDesk._size(None), "—")
        self.assertEqual(SvnDesk._size(0), "0 B")
        self.assertEqual(SvnDesk._size(1024), "1.0 KB")
        self.assertEqual(SvnDesk._size(1048576), "1.0 MB")


class LocalReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_utf8(self):
        (self.root / "file.txt").write_text("你好，SVN\n", encoding="utf-8")
        self.assertEqual(read_preview(self.root, "file.txt"), "你好，SVN\n")

    def test_preview_blocks_traversal(self):
        self.assertIn("之外", read_preview(self.root, "../outside.txt"))

    def test_preview_missing(self):
        self.assertIn("不存在", read_preview(self.root, "missing"))

    def test_preview_directory(self):
        self.assertIn("文件夹", read_preview(self.root, "."))

    def test_preview_binary(self):
        (self.root / "file.bin").write_bytes(b"abc\x00def")
        self.assertIn("二进制", read_preview(self.root, "file.bin"))

    def test_preview_limited(self):
        (self.root / "big.txt").write_text("x" * 50, encoding="utf-8")
        text = read_preview(self.root, "big.txt", limit=10)
        self.assertTrue(text.startswith("x" * 10))
        self.assertIn("截断", text)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX named pipes")
    def test_preview_does_not_open_pipe(self):
        os.mkfifo(self.root / "pipe")
        self.assertIn("不是普通文件", read_preview(self.root, "pipe"))

    def test_list_excludes_administration_and_nested_working_copy(self):
        (self.root / "file.txt").write_text("hello")
        (self.root / ".svn").mkdir()
        (self.root / ".svn" / "wc.db").write_text("private SVN internals")
        (self.root / "nested" / ".svn").mkdir(parents=True)
        (self.root / "nested" / "file.txt").write_text("separate WC")
        files, truncated = local_files(self.root)
        self.assertEqual(files, [("file.txt", 5)])
        self.assertFalse(truncated)

    def test_list_limit(self):
        for i in range(5):
            (self.root / f"file{i}").write_text("a")
        files, truncated = local_files(self.root, limit=3)
        self.assertEqual(len(files), 3)
        self.assertTrue(truncated)

    @unittest.skipIf(os.name == "nt", "symlink creation may require privileges")
    def test_list_does_not_follow_symlink_directory(self):
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "file.txt").write_text("x")
        (folder / "cycle").symlink_to(self.root, target_is_directory=True)
        files, truncated = local_files(self.root)
        self.assertEqual(files, [("folder/file.txt", 1)])
        self.assertFalse(truncated)


class SafetyGateTests(unittest.TestCase):
    @patch("svn_client.ui.messagebox.showinfo")
    def test_demo_rejects_real_operations(self, showinfo):
        app = Mock(_busy=False, demo=True)
        self.assertFalse(SvnDesk._can_operate(app))
        showinfo.assert_called_once()

    @patch("svn_client.ui.messagebox.showinfo")
    def test_busy_rejects_operations(self, showinfo):
        app = Mock(_busy=True, demo=False)
        self.assertFalse(SvnDesk._can_operate(app))
        showinfo.assert_not_called()

    @patch("svn_client.ui.filedialog.askopenfilename")
    @patch("svn_client.ui.messagebox.showinfo")
    def test_demo_does_not_save_executable_settings(self, showinfo, choose):
        app = Mock(_busy=False, demo=True)
        SvnDesk.configure_svn(app)
        choose.assert_not_called()
        app._save_settings.assert_not_called()

    @patch("svn_client.ui.messagebox.showwarning")
    @patch("svn_client.ui.CommitDialog")
    def test_property_conflict_cannot_reach_commit_dialog(self, dialog, warning):
        app = Mock()
        app._can_operate.return_value = True
        app._selected_entries.return_value = [StatusEntry("file", "normal", props="conflicted")]
        SvnDesk.commit_selected(app)
        warning.assert_called_once()
        dialog.assert_not_called()
        app._run.assert_not_called()

    @patch("svn_client.ui.messagebox.askokcancel", return_value=False)
    def test_cancelled_update_does_not_run(self, ask):
        app = Mock()
        app._can_operate.return_value = True
        SvnDesk.update_workspace(app)
        app._run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""UI-independent presentation and safety tests; no display server required."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from svn_client.backend import StatusEntry
from svn_client.ui import (ChangelistDialog, CheckoutDialog, WorkspaceDiscoveryDialog, WorkspaceManagerDialog,
                           SvnDesk, WrapToolbar, directory_entries, fit_window, grouped_entries, has_local_changes,
                           local_files, read_groupable_status, read_preview, status_label)


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
        (self.root / "file.txt").write_bytes("你好，SVN\n".encode("utf-8"))
        self.assertEqual(read_preview(self.root, "file.txt"), "你好，SVN\n")

    def test_preview_preserves_crlf_bytes(self):
        (self.root / "file.txt").write_bytes("你好，SVN\r\n".encode("utf-8"))
        self.assertEqual(read_preview(self.root, "file.txt"), "你好，SVN\r\n")

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


class ChangelistPresentationTests(unittest.TestCase):
    def test_clean_assigned_files_are_members_not_changes(self):
        clean = StatusEntry("clean.txt", "normal", changelist="Task")
        modified = StatusEntry("changed.txt", "modified", changelist="Task")
        self.assertFalse(has_local_changes(clean))
        self.assertTrue(has_local_changes(modified))
        groups = grouped_entries([clean, modified, StatusEntry("unassigned-clean", "normal")])
        self.assertEqual(groups[0], (None, [], []))
        self.assertEqual(len(groups[1][1]), 2)
        self.assertEqual(sum(has_local_changes(entry) for entry in groups[1][1]), 1)

    def test_native_labels_do_not_collide_with_default(self):
        entries = [StatusEntry("a", "modified"), StatusEntry("b", "modified", changelist="未分组（默认）"),
                   StatusEntry("c", "modified", changelist="全部")]
        groups = grouped_entries(entries)
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0][0], None)
        self.assertEqual({group[0] for group in groups}, {None, "未分组（默认）", "全部"})

    def test_filter_does_not_change_full_membership(self):
        entries = [StatusEntry("visible.txt", "modified", changelist="Task"),
                   StatusEntry("hidden.txt", "modified", changelist="Task")]
        groups = grouped_entries(entries, "visible")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0][1]), 2)
        self.assertEqual([entry.path for entry in groups[0][2]], ["visible.txt"])

    def test_filter_matches_group_name_and_status(self):
        entries = [StatusEntry("a", "modified", changelist="中文任务"), StatusEntry("b", "added")]
        self.assertEqual(grouped_entries(entries, "中文")[0][0], "中文任务")
        self.assertEqual(grouped_entries(entries, "已添加")[0][2][0].path, "b")

    def test_existing_group_whitespace_is_preserved(self):
        app = Mock()
        app.name_var.get.return_value = " foo "
        app.files.selection.return_value = ["row"]
        app.entries = {"row": StatusEntry("file.txt", "modified")}
        app.existing = {" foo "}
        ChangelistDialog._accept(app)
        self.assertEqual(app.result, (" foo ", ["file.txt"]))
        app.destroy.assert_called_once()

    @patch("svn_client.ui.messagebox.showwarning")
    def test_cannot_create_an_empty_native_list(self, warning):
        app = Mock()
        app.name_var.get.return_value = "Task"
        app.files.selection.return_value = []
        app.entries = {}
        app.existing = set()
        ChangelistDialog._accept(app)
        app.destroy.assert_not_called()
        warning.assert_called_once()

    def test_group_header_never_becomes_file_target(self):
        app = Mock()
        app.tabs.index.return_value = 0
        app.changes.selection.return_value = ["group0"]
        app._change_lookup = {}
        app._group_lookup = {"group0": "Task"}
        self.assertEqual(SvnDesk._selected_entries(app), [])

    @patch("svn_client.ui.CommitDialog")
    def test_group_commit_reviews_all_members_even_hidden_and_clean(self, dialog):
        app = Mock()
        app._can_operate.return_value = True
        app.tabs.index.return_value = 0
        app.changes.selection.return_value = ["group0"]
        app._group_lookup = {"group0": "Task"}
        app._change_lookup = {}
        app.entries = [StatusEntry("visible", "modified", changelist="Task"),
                       StatusEntry("hidden", "modified", changelist="Task"),
                       StatusEntry("clean", "normal", changelist="Task"),
                       StatusEntry("other", "modified", changelist="Other")]
        dialog.return_value.result = "Commit message"
        SvnDesk.commit_changelist(app)
        self.assertEqual(dialog.call_args.args[1], ["clean", "hidden", "visible"])
        self.assertEqual(dialog.call_args.kwargs["changelist"], "Task")
        app._run.call_args.args[1]()
        app.client.commit_changelist.assert_called_once_with("Task", "Commit message", expected_paths=["clean", "hidden", "visible"])

    @patch("svn_client.ui.messagebox.showinfo")
    @patch("svn_client.ui.CommitDialog")
    def test_default_group_cannot_submit_entire_workspace(self, dialog, info):
        app = Mock()
        app._can_operate.return_value = True
        app.tabs.index.return_value = 0
        app.changes.selection.return_value = ["group0"]
        app._group_lookup = {"group0": None}
        app._change_lookup = {}
        SvnDesk.commit_changelist(app)
        dialog.assert_not_called()
        app._run.assert_not_called()

    @patch("svn_client.ui.messagebox.showinfo")
    def test_clean_only_group_cannot_commit(self, info):
        app = Mock()
        app._can_operate.return_value = True
        app.tabs.index.return_value = 0
        app.changes.selection.return_value = ["group0"]
        app._group_lookup = {"group0": "Task"}
        app._change_lookup = {}
        app.entries = [StatusEntry("clean", "normal", changelist="Task")]
        SvnDesk.commit_changelist(app)
        app._run.assert_not_called()

    @patch("svn_client.ui.messagebox.askokcancel", return_value=True)
    def test_unassign_changes_labels_only(self, confirm):
        app = Mock()
        app._can_operate.return_value = True
        app._selected_entries.return_value = [StatusEntry("a", "modified", changelist="Task"), StatusEntry("b", "modified")]
        SvnDesk.remove_from_changelist(app)
        app._run.call_args.args[1]()
        app.client.remove_changelist.assert_called_once_with(["a"])
        app.client.revert.assert_not_called()

    def test_groupable_scan_requests_verbose_and_omits_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "folder").mkdir()
            (root / "clean").write_text("x")
            client = Mock()
            client.status.return_value = [StatusEntry("folder", "normal"),
                                          StatusEntry("clean", "normal", changelist="Task"),
                                          StatusEntry("outside", "normal", file_external=True)]
            pending, candidates = read_groupable_status(client, root)
            client.status.assert_called_once_with(verbose=True)
            self.assertEqual([entry.path for entry in pending], ["clean"])
            self.assertEqual([entry.path for entry in candidates], ["clean"])

    def test_filter_clears_hidden_file_selections(self):
        class FakeTree:
            def __init__(self):
                self.nodes = {}
                self.selected = []
            def selection(self):
                return self.selected[:]
            def get_children(self):
                return [iid for iid, node in self.nodes.items() if not node["parent"]]
            def item(self, iid, option):
                return self.nodes[iid][option]
            def delete(self, *items):
                self.nodes.clear()
                self.selected.clear()
            def insert(self, parent, position, iid, **options):
                self.nodes[iid] = {"parent": parent, **options}
            def selection_add(self, iid):
                self.selected.append(iid)
        app = Mock()
        app._filter_job = None
        app._change_lookup = {}
        app._group_lookup = {}
        app.changes = FakeTree()
        app.filter_var.get.return_value = ""
        app.entries = [StatusEntry("one.txt", "modified", changelist="Task"),
                       StatusEntry("two.txt", "modified", changelist="Task")]
        SvnDesk._render_changes(app)
        one = next(iid for iid, entry in app._change_lookup.items() if entry.path == "one.txt")
        app.changes.selection_add(one)
        app.filter_var.get.return_value = "two"
        SvnDesk._render_changes(app)
        self.assertEqual(app.changes.selection(), [])
        self.assertEqual([entry.path for entry in app._change_lookup.values()], ["two.txt"])
        self.assertEqual(len(app.entries), 2)

    def test_filter_debounces_repeated_typing(self):
        app = Mock()
        app._filter_job = "old-job"
        app.after.return_value = "new-job"
        SvnDesk._schedule_filter(app)
        app.after_cancel.assert_called_once_with("old-job")
        app.after.assert_called_once_with(180, app._render_changes)
        self.assertEqual(app._filter_job, "new-job")

    def test_dpi_window_fits_available_screen(self):
        from svn_client.display import DisplayMetrics
        window = Mock()
        window.winfo_screenwidth.return_value = 800
        window.winfo_screenheight.return_value = 600
        fit_window(window, DisplayMetrics("ui", "mono", 2, 192), 1320, 850, 1030, 680)
        window.geometry.assert_called_once_with("760x520")
        window.minsize.assert_called_once_with(760, 520)


class NavigationTests(unittest.TestCase):
    def test_all_self_calls_and_registered_handlers_exist(self):
        """Catch incomplete UI snapshots before a display is available."""
        import ast
        import inspect
        for cls in (SvnDesk, ChangelistDialog, CheckoutDialog, WorkspaceDiscoveryDialog, WorkspaceManagerDialog, WrapToolbar):
            tree = ast.parse(inspect.getsource(cls))
            targets = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                    targets.add(node.func.attr)
                callbacks = [kw.value for kw in node.keywords if kw.arg == "command"]
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"bind", "protocol"}:
                    callbacks += node.args[1:2]
                for callback in callbacks:
                    if isinstance(callback, ast.Attribute) and isinstance(callback.value, ast.Name) and callback.value.id == "self":
                        targets.add(callback.attr)
            self.assertEqual(sorted(name for name in targets if not callable(getattr(cls, name, None))), [])

    def test_directory_listing_is_one_level_and_excludes_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".svn").mkdir()
            (root / "folder").mkdir()
            (root / "folder" / "deep.txt").write_text("hidden until expanded")
            (root / "file.txt").write_text("x")
            (root / "nested" / ".svn").mkdir(parents=True)
            rows = directory_entries(root, root)
            self.assertEqual([(row[0], row[1]) for row in rows], [("folder", "dir"), ("file.txt", "file")])
            with self.assertRaises(ValueError):
                directory_entries(root, root / "nested")

    def test_directory_listing_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                directory_entries(root, root.parent)

    def test_directory_navigation_blocks_admin_case_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".SvN").mkdir()
            self.assertEqual(directory_entries(root, root), [])
            with self.assertRaises(ValueError):
                directory_entries(root, root / ".SvN")

    def test_directory_listing_rejects_nested_working_copy_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested" / ".svn").mkdir(parents=True)
            (root / "nested" / "child").mkdir()
            with self.assertRaises(ValueError):
                directory_entries(root, root / "nested" / "child")

    @unittest.skipIf(os.name == "nt", "symlink creation may require privileges")
    def test_directory_listing_rejects_cached_internal_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real" / "child").mkdir(parents=True)
            (root / "alias").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(ValueError):
                directory_entries(root, root / "alias")
            with self.assertRaises(ValueError):
                directory_entries(root, root / "alias" / "child")

    def test_directory_listing_has_no_arbitrary_file_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3101):
                (root / f"file{index:04d}").touch()
            self.assertEqual(len(directory_entries(root, root)), 3101)

    def test_tree_inserts_only_one_page(self):
        app = Mock()
        app._workspace_cache = {"root": [(f"file{i}", "file", i) for i in range(550)]}
        app._workspace_nodes = {}
        app._workspace_more = {}
        app.workspace_tree.get_children.return_value = []
        app.workspace_tree.insert.side_effect = [f"node{i}" for i in range(201)]
        SvnDesk._append_workspace_page(app, "root", 0)
        self.assertEqual(len(app._workspace_nodes), 200)
        self.assertEqual(app._workspace_more, {"node200": ("root", 200)})

    def test_obsolete_workspace_reads_are_ignored(self):
        import queue
        app = Mock()
        app._tree_generation = 2
        app._tree_queue = queue.Queue()
        app._tree_queue.put((1, "old-root", [("private", "file", 1)], None))
        app._workspace_cache = {}
        SvnDesk._drain_tree_queue(app)
        self.assertEqual(app._workspace_cache, {})
        app.workspace_tree.insert.assert_not_called()
        app._show_workspace_folder.assert_not_called()

    def test_workspace_directory_selection_is_navigation_only(self):
        app = Mock()
        app._preview_request = 0
        app.workspace_tree.selection.return_value = ("folder",)
        app._workspace_nodes = {"folder": ("src", "dir")}
        SvnDesk._workspace_selected(app)
        app._start_directory_load.assert_called_once_with("folder")
        app.select_tab.assert_called_once_with(1)
        app.client.commit.assert_not_called()
        app.changes.selection_set.assert_not_called()

    def test_stale_file_preview_does_not_replace_new_selection(self):
        app = Mock(_busy=False, demo=False, _preview_request=1)
        SvnDesk._preview_local_path(app, "old.txt")
        callback = app._run.call_args.args[2]
        app._preview_request = 2
        callback("old content")
        app._show_preview.assert_not_called()

    def test_any_new_detail_panel_invalidates_pending_content(self):
        app = Mock(_busy=False, demo=False, _preview_request=1)
        SvnDesk._preview_local_path(app, "old.txt")
        callback = app._run.call_args.args[2]
        SvnDesk._show_preview(app, "Shelf / Help / Repository details", "New content")
        self.assertEqual(app._preview_request, 2)
        callback("old content")
        app._show_preview.assert_not_called()
        app.preview.insert.assert_called_once_with("end", "New content", "")

    def test_group_header_disables_file_actions(self):
        app = Mock(_busy=False, demo=False)
        app.tabs.index.return_value = 0
        app.changes.selection.return_value = ("group",)
        app._group_lookup = {"group": "Task"}
        app._change_lookup = {}
        app.entries = [StatusEntry("file", "modified", changelist="Task")]
        self.assertFalse(SvnDesk._action_available(app, "commit_files"))
        self.assertFalse(SvnDesk._action_available(app, "move"))
        self.assertTrue(SvnDesk._action_available(app, "commit_group"))

    def test_open_refresh_do_not_scan_entire_filesystem(self):
        import inspect
        self.assertNotIn("local_files(", inspect.getsource(SvnDesk.open_workspace))
        self.assertNotIn("local_files(", inspect.getsource(SvnDesk.refresh))

    @patch("svn_client.ui.ttk.Frame")
    def test_toolbar_wraps_actions_on_narrow_window(self, create_frame):
        toolbar = Mock()
        toolbar.winfo_width.return_value = 180
        toolbar.last_width = 0
        toolbar.rows = []
        rows = [Mock(), Mock(), Mock()]
        create_frame.side_effect = rows
        toolbar.buttons = [Mock(), Mock(), Mock()]
        for button in toolbar.buttons:
            button.winfo_reqwidth.return_value = 90
        WrapToolbar._flow(toolbar)
        self.assertEqual([button.pack.call_args.kwargs["in_"] for button in toolbar.buttons], rows)

    @patch("svn_client.ui.ttk.Frame")
    def test_toolbar_uneven_buttons_use_independent_rows(self, create_frame):
        toolbar = Mock()
        toolbar.winfo_width.return_value = 180
        toolbar.last_width = 0
        toolbar.rows = []
        rows = [Mock(), Mock()]
        create_frame.side_effect = rows
        toolbar.buttons = [Mock(), Mock(), Mock(), Mock()]
        for button, width in zip(toolbar.buttons, [43, 113, 103, 53]):
            button.winfo_reqwidth.return_value = width
        WrapToolbar._flow(toolbar)
        self.assertEqual([button.pack.call_args.kwargs["in_"] for button in toolbar.buttons], [rows[0], rows[0], rows[1], rows[1]])
        for button in toolbar.buttons:
            button.grid.assert_not_called()
            button.lift.assert_called_once_with()

    def test_branch_refresh_purges_all_descendant_state(self):
        app = Mock()
        graph = {"root": ("folder", "more"), "folder": ("file",), "file": (), "more": ()}
        app.workspace_tree.get_children.side_effect = lambda node: graph[node]
        app._workspace_nodes = {"root": (".", "dir"), "folder": ("dir", "dir"), "file": ("dir/a", "file")}
        app._workspace_cache = {"root": [], "folder": [1, 2]}
        app._workspace_pending = {"folder"}
        app._workspace_more = {"more": ("root", 200)}
        SvnDesk._purge_workspace_children(app, "root")
        self.assertEqual(app._workspace_nodes, {"root": (".", "dir")})
        self.assertEqual(app._workspace_cache, {"root": []})
        self.assertEqual(app._workspace_pending, set())
        self.assertEqual(app._workspace_more, {})

    def test_switching_workspaces_clears_same_relative_path_targets(self):
        app = Mock(_preview_request=0)
        app.changes.selection.return_value = ("old-file",)
        app.files.selection.return_value = ("old-local",)
        app._change_lookup = {"old-file": StatusEntry("same.txt", "modified", changelist="Task")}
        app._group_lookup = {"old-group": "Task"}
        app._file_lookup = {"old-local": "same.txt"}
        app._file_kind = {"old-local": "file"}
        SvnDesk._clear_workspace_selection(app)
        self.assertEqual(app._change_lookup, {})
        self.assertEqual(app._group_lookup, {})
        self.assertEqual(app._file_lookup, {})
        app.changes.selection_remove.assert_called_once_with("old-file")
        app.files.selection_remove.assert_called_once_with("old-local")

    def test_keyboard_activation_is_wired(self):
        import inspect
        source = inspect.getsource(SvnDesk._build_ui)
        for name in ("workspace_tree", "repository_tree", "changes", "files", "repository"):
            self.assertIn(f'self.{name}.bind("<Return>"', source)
        callback = Mock()
        self.assertEqual(SvnDesk._activate_key(callback), "break")
        callback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

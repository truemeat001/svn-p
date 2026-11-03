import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from svn_client.workspaces import (load_settings, normalize_settings, path_key,
    remember_workspace, remove_workspace, rename_workspace, save_settings,
    startup_workspace, validate_name)
from svn_client.ui import SvnDesk


class WorkspaceRegistryTests(unittest.TestCase):
    def test_legacy_last_directory_is_migrated(self):
        settings = normalize_settings({"last_workspace": "C:\\Projects\\Game"})
        self.assertEqual(len(settings["workspaces"]), 1)
        record = startup_workspace(settings)
        self.assertEqual(record["name"], "Game")
        self.assertEqual(record["path"], "C:\\Projects\\Game")

    def test_only_allowlisted_noncredential_fields_survive(self):
        settings = normalize_settings({"password": "secret", "token": "token", "repository_url": "https://x:y@host/repo",
            "last_workspace": "https://user:pass@host/repo", "svn_executable": "svn",
            "workspaces": [{"id": "one", "name": "Project", "path": "/tmp/project", "password": "hidden", "url": "https://x:y@h"}]})
        payload = json.dumps(settings)
        for text in ("secret", "token", "password", "repository_url", "https://", "hidden"):
            self.assertNotIn(text, payload)
        self.assertEqual(settings["svn_executable"], "svn")

    def test_windows_aliases_deduplicate(self):
        settings = normalize_settings({"workspaces": [{"id": "a", "name": "one", "path": "C:\\Repo"},
                                                     {"id": "b", "name": "two", "path": "c:/repo/"}]})
        self.assertEqual(len(settings["workspaces"]), 1)
        self.assertEqual(path_key("C:\\Repo"), path_key("c:/repo/"))

    def test_missing_path_is_retained_for_repair(self):
        settings = normalize_settings({"last_workspace": "/missing/not-created/project"})
        self.assertEqual(startup_workspace(settings)["path"], "/missing/not-created/project")

    def test_invalid_relative_registry_paths_are_discarded(self):
        settings = normalize_settings({"last_workspace": "relative", "workspaces": [{"path": "../other"}]})
        self.assertEqual(settings["workspaces"], [])

    def test_register_reuses_identity_and_name(self):
        settings = normalize_settings({})
        first = remember_workspace(settings, "/tmp/project", name="我的项目")
        second = remember_workspace(settings, "/tmp/project")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["name"], "我的项目")
        self.assertEqual(len(settings["workspaces"]), 1)

    def test_relink_changes_record_not_identity(self):
        settings = normalize_settings({})
        first = remember_workspace(settings, "/tmp/old", name="Project")
        identifier = first["id"]
        record = remember_workspace(settings, "/tmp/new", record_id=identifier)
        self.assertEqual(record, {"id": identifier, "name": "Project", "path": "/tmp/new"})

    def test_relink_to_other_registered_root_rejects_without_mutation(self):
        settings = normalize_settings({})
        first = remember_workspace(settings, "/tmp/one")
        remember_workspace(settings, "/tmp/two")
        before = copy.deepcopy(settings)
        with self.assertRaises(ValueError):
            remember_workspace(settings, "/tmp/two", record_id=first["id"])
        self.assertEqual(settings, before)

    def test_rename_does_not_move_or_change_path(self):
        settings = normalize_settings({})
        record = remember_workspace(settings, "/tmp/one")
        rename_workspace(settings, record["id"], "新名称")
        self.assertEqual(record["path"], "/tmp/one")
        self.assertEqual(record["name"], "新名称")

    def test_remove_deletes_only_registry_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            file = root / "precious.txt"
            file.write_text("keep")
            settings = normalize_settings({})
            record = remember_workspace(settings, str(root))
            remove_workspace(settings, record["id"])
            self.assertEqual(file.read_text(), "keep")
            self.assertEqual(settings["workspaces"], [])
            self.assertNotIn("last_workspace", settings)
            self.assertIsNone(startup_workspace(settings))

    def test_registration_without_open_does_not_change_last_workspace(self):
        settings = normalize_settings({})
        first = remember_workspace(settings, "/tmp/open")
        remember_workspace(settings, "/tmp/discovered", set_last=False)
        self.assertEqual(startup_workspace(settings)["id"], first["id"])

    def test_restore_preference_is_respected(self):
        settings = normalize_settings({"last_workspace": "/tmp/project", "restore_last_workspace": False})
        self.assertIsNone(startup_workspace(settings))

    def test_atomic_roundtrip_with_chinese_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            settings = normalize_settings({})
            remember_workspace(settings, "/tmp/project", name="游戏工程")
            save_settings(path, settings)
            self.assertEqual(load_settings(path), settings)
            self.assertEqual(list(Path(tmp).glob(".settings-*.tmp")), [])

    def test_failed_replace_retains_original_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text('{"original": true}')
            with patch("svn_client.workspaces.os.replace", side_effect=OSError("denied")):
                with self.assertRaises(OSError):
                    save_settings(path, normalize_settings({}))
            self.assertEqual(path.read_text(), '{"original": true}')
            self.assertEqual(list(Path(tmp).glob(".settings-*.tmp")), [])

    def test_invalid_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("broken")
            with self.assertRaises(ValueError):
                load_settings(path)
            self.assertEqual(path.read_text(), "broken")

    def test_invalid_names_rejected(self):
        for name in ("", "  ", "a\nb", "x" * 121, None, "https://user:secret@host"):
            with self.assertRaises(ValueError):
                validate_name(name)


class WorkspaceFlowTests(unittest.TestCase):
    def test_startup_restores_named_last_workspace(self):
        settings = normalize_settings({"last_workspace": "/tmp/game"})
        app = Mock(demo=False, _busy=False, client=None, _manual_open_requested=False, settings=settings)
        SvnDesk._restore_saved_workspace(app)
        app.open_workspace.assert_called_once_with("/tmp/game", record_id=settings["last_workspace_id"], auto_restore=True)

    def test_explicit_open_wins_startup_race(self):
        app = Mock(demo=False, _busy=False, client=None, _manual_open_requested=True,
                   settings=normalize_settings({"last_workspace": "/tmp/old"}))
        SvnDesk._restore_saved_workspace(app)
        app.open_workspace.assert_not_called()

    @patch("svn_client.ui.save_settings")
    def test_demo_never_saves_settings_or_restores_real_workspace(self, save):
        app = Mock(demo=True)
        self.assertFalse(SvnDesk._save_settings(app))
        SvnDesk._restore_saved_workspace(app)
        save.assert_not_called()
        app.open_workspace.assert_not_called()

    def test_demo_constructor_bypasses_real_settings_read(self):
        import ast
        import inspect
        import textwrap
        method = ast.parse(textwrap.dedent(inspect.getsource(SvnDesk.__init__)))
        assignment = next(node for node in ast.walk(method) if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Attribute) and target.attr == "settings" for target in node.targets))
        self.assertIsInstance(assignment.value, ast.IfExp)
        self.assertEqual(assignment.value.test.id, "demo")
        self.assertEqual(assignment.value.body.func.id, "normalize_settings")
        self.assertEqual(assignment.value.body.args[0].keys, [])
        self.assertEqual(assignment.value.orelse.func.attr, "_read_settings")

    @patch("svn_client.ui.messagebox.showwarning")
    def test_open_failure_keeps_active_workspace_and_record(self, warning):
        settings = normalize_settings({})
        active = remember_workspace(settings, "/tmp/active")
        missing = remember_workspace(settings, "/tmp/missing", set_last=False)
        before = copy.deepcopy(settings)
        old_client = Mock()
        app = Mock(_busy=False, demo=False, client=old_client, root_path=Path("/tmp/active"),
                   settings=settings, _active_workspace_id=active["id"], _workspace_errors={})
        SvnDesk.open_workspace(app, "/tmp/missing", record_id=missing["id"])
        app._run.call_args.kwargs["on_error"](FileNotFoundError("Missing folder"))
        self.assertIs(app.client, old_client)
        self.assertEqual(app.settings, before)
        self.assertEqual(app._active_workspace_id, active["id"])
        self.assertIn(missing["id"], app._workspace_errors)

    @patch("svn_client.ui.save_settings", side_effect=OSError("read-only"))
    @patch("svn_client.ui.messagebox.showwarning")
    def test_save_failure_is_not_reported_as_success(self, warning, save):
        app = Mock(demo=False, settings=normalize_settings({}), _settings_warning=None)
        self.assertFalse(SvnDesk._save_settings(app))
        warning.assert_called_once()

    @patch("svn_client.ui.filedialog.askdirectory")
    def test_open_dialog_cancels_restore_before_nested_event_loop(self, choose):
        app = Mock(demo=False, _busy=False, client=None, _manual_open_requested=False, root_path=None,
                   settings=normalize_settings({"last_workspace": "/tmp/old"}))
        def modal(**kwargs):
            SvnDesk._restore_saved_workspace(app)
            return ""
        choose.side_effect = modal
        SvnDesk.choose_workspace(app)
        app.open_workspace.assert_not_called()
        self.assertTrue(app._manual_open_requested)

    @patch("svn_client.ui.WorkspaceManagerDialog")
    def test_manager_cancels_restore_before_nested_event_loop(self, manager):
        app = Mock(demo=False, _busy=False, client=None, _manual_open_requested=False,
                   settings=normalize_settings({"last_workspace": "/tmp/old"}))
        def modal(parent):
            SvnDesk._restore_saved_workspace(parent)
            return Mock(result=("close", None, True))
        manager.side_effect = modal
        SvnDesk.manage_workspaces(app)
        app.open_workspace.assert_not_called()

    @patch("svn_client.ui.messagebox.askyesno", return_value=False)
    @patch("svn_client.ui.save_settings")
    def test_corrupt_settings_not_overwritten_without_confirmation(self, save, confirm):
        app = Mock(demo=False, _settings_warning="invalid JSON")
        self.assertFalse(SvnDesk._save_settings(app))
        save.assert_not_called()

    @patch("svn_client.ui.discover_svn", return_value="svn")
    @patch("svn_client.ui.SvnClient")
    @patch("svn_client.ui.ShelfManager")
    @patch("svn_client.ui.read_groupable_status", return_value=([], []))
    def test_successful_open_registers_named_workspace_after_validation(self, status, shelf, client, program):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client.return_value.info.return_value = {"wc_root": str(root), "url": "https://host/repo"}
            shelf.return_value.list.return_value = []
            app = Mock(_busy=False, demo=False, client=None, root_path=None,
                       settings=normalize_settings({}), _workspace_errors={})
            app.repository.get_children.return_value = ()
            SvnDesk.open_workspace(app, str(root), workspace_name="游戏项目")
            work, complete = app._run.call_args.args[1:3]
            self.assertEqual(app.settings["workspaces"], [])
            complete(work())
            saved = startup_workspace(app.settings)
            self.assertEqual(saved["name"], "游戏项目")
            self.assertEqual(saved["path"], str(root))
            self.assertEqual(app._active_workspace_id, saved["id"])
            self.assertIs(app.client, client.return_value)
            app._save_settings.assert_called_once_with()

    @patch("svn_client.ui.discover_svn", return_value="svn")
    @patch("svn_client.ui.SvnClient")
    @patch("svn_client.ui.ShelfManager")
    @patch("svn_client.ui.read_groupable_status", return_value=([], []))
    def test_successful_relink_preserves_record_identity_and_old_files(self, status, shelf, client, program):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "old", Path(tmp) / "new"
            old.mkdir(); new.mkdir()
            (old / "keep.txt").write_text("keep")
            settings = normalize_settings({})
            record = remember_workspace(settings, str(old), name="Stable name")
            identifier = record["id"]
            client.return_value.info.return_value = {"wc_root": str(new), "url": "https://host/repo"}
            shelf.return_value.list.return_value = []
            app = Mock(_busy=False, demo=False, root_path=old, settings=settings, _workspace_errors={})
            app.repository.get_children.return_value = ()
            SvnDesk.open_workspace(app, str(new), record_id=identifier)
            work, complete = app._run.call_args.args[1:3]
            complete(work())
            self.assertEqual(app.settings["workspaces"], [{"id": identifier, "name": "Stable name", "path": str(new)}])
            self.assertEqual((old / "keep.txt").read_text(), "keep")

    @patch("svn_client.ui.CheckoutDialog")
    @patch("svn_client.ui.messagebox.askokcancel", return_value=False)
    def test_checkout_cancel_never_starts_download_or_registers(self, confirmation, dialog):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "new"
            dialog.return_value.result = ("New", "https://host/repo", str(destination))
            settings = normalize_settings({"last_workspace": "/tmp/current"})
            before = copy.deepcopy(settings)
            app = Mock(_busy=False, demo=False, settings=settings)
            SvnDesk.create_workspace(app)
            checked = app._run.call_args.args[2]
            checked(("https://host/repo", destination))
            self.assertEqual(app._run.call_count, 1)
            self.assertEqual(app.settings, before)
            self.assertFalse(destination.exists())
            app.open_workspace.assert_not_called()
            app._save_settings.assert_not_called()

    @patch("svn_client.ui.CheckoutDialog")
    @patch("svn_client.ui.messagebox.askokcancel", return_value=True)
    @patch("svn_client.ui.messagebox.showerror")
    def test_checkout_failure_keeps_partial_files_and_current_workspace(self, error_dialog, confirmation, dialog):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "new"
            destination.mkdir()
            partial = destination / "partial.txt"
            partial.write_text("retained")
            dialog.return_value.result = ("New", "https://host/repo", str(destination))
            settings = normalize_settings({"last_workspace": "/tmp/current"})
            before = copy.deepcopy(settings)
            old_client = Mock()
            app = Mock(_busy=False, demo=False, settings=settings, root_path=Path("/tmp/current"), client=old_client)
            SvnDesk.create_workspace(app)
            app._run.call_args.args[2](("https://host/repo", destination))
            self.assertEqual(app._run.call_count, 2)
            app._run.call_args.kwargs["on_error"](RuntimeError("Interrupted checkout"))
            self.assertEqual(partial.read_text(), "retained")
            self.assertEqual(app.settings, before)
            self.assertIs(app.client, old_client)
            app.open_workspace.assert_not_called()
            app._save_settings.assert_not_called()

    @patch("svn_client.ui.discover_svn", return_value="svn")
    @patch("svn_client.ui.SvnClient")
    def test_discovery_registers_valid_roots_independently_without_changing_last(self, client, program):
        settings = normalize_settings({})
        current = remember_workspace(settings, "/tmp/current", name="Current")
        previous_id = current["id"]
        good = Mock()
        good.info.return_value = {"wc_root": "/tmp/good"}
        bad = Mock()
        bad.info.side_effect = ValueError("Not a working copy")
        client.side_effect = [good, bad]
        old_client = Mock()
        app = Mock(settings=settings, client=old_client)
        app._save_settings.return_value = True
        SvnDesk._register_discovered_paths(app, ["/tmp/good", "/tmp/bad"])
        work, complete = app._run.call_args.args[1:3]
        complete(work())
        self.assertEqual({r["path"] for r in app.settings["workspaces"]}, {"/tmp/current", "/tmp/good"})
        self.assertEqual(app.settings["last_workspace_id"], previous_id)
        self.assertIs(app.client, old_client)
        old_client.update.assert_not_called()
        old_client.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

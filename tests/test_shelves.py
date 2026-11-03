"""Shelf safety tests; real SVN round trips run when svn/svnadmin are installed."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from svn_client.shelves import ShelfConflict, ShelfError, ShelfManager, _safe_relative


def enc(data):
    return base64.b64encode(data).decode("ascii")


class FakeManager(ShelfManager):
    def __init__(self, wc, store):
        super().__init__(wc, store)
        self.states = {".": {"item": "normal", "kind": "dir"}}
        self.base_data = {}
        self.base_props = {}
        self.current_props = {}
        self.revision = "1"
        self.repo_url = "file:///test/repo/trunk"
        self.commands = []

    def node(self, rel, item, kind="file", content=b"base", base=b"base", props=None, base_props=None, **flags):
        self.states[rel] = {"item": item, "kind": kind, **flags}
        path = self.working_copy / rel
        if item != "deleted":
            if kind == "dir":
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        self.base_data[rel] = base
        self.base_props[rel] = base_props or {}
        self.current_props[rel] = props or {}

    def _info(self, path="."):
        node = self.states.get(path, {"kind": "file", "item": "normal"})
        return {"kind": node["kind"], "revision": self.revision,
                "url": self.repo_url if path == "." else self.repo_url + "/" + path,
                "uuid": "test-uuid", "schedule": "normal", "depth": "infinity"}

    def _properties(self, path, base=False):
        return copy.deepcopy((self.base_props if base else self.current_props).get(path, {}))

    def _status(self, paths=None):
        result = []
        for path, node in sorted(self.states.items()):
            if paths and not any(p == "." or path == p or path.startswith(p + "/") for p in paths):
                continue
            props = "modified" if self.current_props.get(path, {}) != self.base_props.get(path, {}) else "none"
            result.append({"path": path, "item": node["item"], "props": props,
                           **{k: v for k, v in node.items() if k not in ("item", "kind")}})
        return result

    def _base(self, path, info=None):
        info = info or self._info(path)
        result = {k: info[k] for k in ("kind", "revision", "url", "uuid")}
        result["properties"] = self._properties(path, base=True)
        if info["kind"] == "file":
            result["sha256"] = hashlib.sha256(self.base_data[path]).hexdigest()
            result["working_sha256"] = result["sha256"]
        return result

    def _svn(self, *args):
        self.commands.append(args)
        command = args[0]
        if command == "delete":
            rel = args[-1][:-1]
            (self.working_copy / rel).unlink()
            self.states[rel]["item"] = "deleted"
        elif command == "add":
            rel = args[-1][:-1]
            self.states[rel] = {"item": "added", "kind": "dir" if (self.working_copy / rel).is_dir() else "file"}
        elif command == "propdel":
            self.current_props.setdefault(args[-1][:-1], {}).pop(args[2], None)
        elif command == "propset":
            self.current_props.setdefault(args[-1][:-1], {})[args[4]] = enc(Path(args[2]).read_bytes())
        else:
            raise AssertionError(f"Unexpected command: {args}")
        return b""

    def clean(self):
        for rel, node in list(self.states.items()):
            if node["item"] in ("added", "unversioned"):
                path = self.working_copy / rel
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                del self.states[rel]
                self.current_props.pop(rel, None)
            else:
                if node["kind"] == "file":
                    (self.working_copy / rel).write_bytes(self.base_data[rel])
                self.states[rel]["item"] = "normal"
                self.current_props[rel] = copy.deepcopy(self.base_props.get(rel, {}))


class ShelvesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wc = self.root / "wc"
        self.wc.mkdir()
        self.store = self.root / "shelves"
        self.manager = FakeManager(self.wc, self.store)

    def tearDown(self):
        self.temp.cleanup()

    def simple(self):
        self.manager.node("file.bin", "modified", content=b"changed\x00\xff\r\n")
        return self.manager.create("Test backup")

    def manifest_path(self, shelf):
        return self.store / shelf["id"] / "manifest.json"

    def mutate_manifest(self, shelf, fn):
        path = self.manifest_path(shelf)
        data = json.loads(path.read_text())
        fn(data)
        path.write_text(json.dumps(data))

    def test_compressed_large_file_and_binary_properties_roundtrip(self):
        content = b"line\x00\xff\r\n" * (384 * 1024)
        properties = {"custom:binary": enc(bytes(range(256)))}
        self.manager.node("large.bin", "modified", content=content, props=properties)
        shelf = self.manager.create("compressed")
        manifest = json.loads(self.manifest_path(shelf).read_text())
        entry = manifest["entries"][0]
        self.assertEqual(manifest["format"], 2)
        self.assertEqual(entry["blob_encoding"], "zlib")
        self.assertLess(entry["stored_size"], len(content) // 100)
        self.assertEqual((self.store / shelf["id"] / "blobs" / entry["blob"]).stat().st_size, entry["stored_size"])
        self.manager.clean()
        self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "large.bin").read_bytes(), content)
        self.assertEqual(self.manager.current_props["large.bin"], properties)

    def test_incompressible_small_and_empty_files_use_raw_fallback(self):
        contents = {"random.bin": random.Random(42).randbytes(2 * 1024 * 1024),
                    "tiny.txt": b"tiny" * 20, "empty": b""}
        for name, content in contents.items():
            self.manager.node(name, "modified", content=content)
        shelf = self.manager.create("raw fallback")
        entries = self.manager.details(shelf["id"])["entries"]
        for entry in entries:
            self.assertEqual(entry["blob_encoding"], "raw")
            self.assertEqual(entry["stored_size"], len(contents[entry["path"]]))
        self.manager.clean()
        self.manager.restore(shelf["id"])
        for name, content in contents.items():
            self.assertEqual((self.wc / name).read_bytes(), content)

    def test_representative_synthetic_storage_savings(self):
        size = 4 * 1024 * 1024
        line = b'{"asset":"Characters/Hero","state":"modified","revision":1234}\r\n'
        random_data = random.Random(42).randbytes(size)
        samples = {"structured_text": (line * (size // len(line) + 1))[:size],
                   "repeated_binary": bytes(range(256)) * (size // 256),
                   "random_binary": random_data,
                   "already_compressed": zlib.compress(random_data, 6)}
        for name, content in samples.items():
            self.manager.node(name, "modified", content=content)
        shelf = self.manager.create("synthetic storage measurement")
        entries = self.manager.details(shelf["id"])["entries"]
        for entry in entries:
            if entry["path"] in ("structured_text", "repeated_binary"):
                self.assertEqual(entry["blob_encoding"], "zlib")
                self.assertLess(entry["stored_size"], entry["size"] // 100)
            else:
                self.assertEqual(entry["blob_encoding"], "raw")
                self.assertEqual(entry["stored_size"], entry["size"])
        self.manager.clean()
        self.manager.restore(shelf["id"])
        for name, content in samples.items():
            self.assertEqual((self.wc / name).read_bytes(), content)

    def test_duplicate_compressed_content_uses_one_blob(self):
        content = b"shared bytes\x00\xff" * 10000
        self.manager.node("first", "modified", content=content)
        self.manager.node("second", "modified", content=content)
        shelf = self.manager.create("deduplicated within shelf")
        self.assertEqual(len(list((self.store / shelf["id"] / "blobs").iterdir())), 1)
        self.manager.clean()
        self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "first").read_bytes(), content)
        self.assertEqual((self.wc / "second").read_bytes(), content)

    def test_legacy_format_one_is_readable_without_rewrite(self):
        content = b"legacy\x00\xff\r\n" * 10000
        self.manager.node("legacy.bin", "modified", content=content,
                          props={"binary": enc(b"\x00\xff\r\n")})
        shelf = self.manager.create("legacy fixture")
        manifest = json.loads(self.manifest_path(shelf).read_text())
        manifest["format"] = 1
        for entry in manifest["entries"]:
            (self.store / shelf["id"] / "blobs" / entry["blob"]).write_bytes(content)
            del entry["blob_encoding"], entry["stored_size"]
        self.manifest_path(shelf).write_text(json.dumps(manifest))
        before = self.manifest_path(shelf).read_bytes()
        self.assertEqual(self.manager.list()[0]["id"], shelf["id"])
        self.manager.details(shelf["id"])
        self.manager.clean()
        self.manager.preflight_restore(shelf["id"])
        self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "legacy.bin").read_bytes(), content)
        self.assertEqual(self.manager.current_props["legacy.bin"]["binary"], enc(b"\x00\xff\r\n"))
        self.assertEqual(self.manifest_path(shelf).read_bytes(), before)

    def test_large_file_paths_do_not_use_read_bytes(self):
        self.manager.node("large", "modified", content=b"abc\x00\xff" * (512 * 1024))
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded file read")):
            self.manager.preflight_create()
            shelf = self.manager.create("streaming")
            self.manager.details(shelf["id"])
            self.manager.clean()
            self.manager.preflight_restore(shelf["id"])
            self.manager.restore(shelf["id"])

    def test_compressed_corruption_truncation_and_trailing_streams_are_refused(self):
        self.manager.node("large", "modified", content=b"compressible\x00\xff" * 10000)
        shelf = self.manager.create("validate compression")
        self.manager.clean()
        manifest = json.loads(self.manifest_path(shelf).read_text())
        entry = manifest["entries"][0]
        blob = self.store / shelf["id"] / "blobs" / entry["blob"]
        original = blob.read_bytes()
        variants = (original[:-1], original + b"garbage", original + zlib.compress(b"second stream"),
                    b"\x00" + original[1:])
        for content in variants:
            with self.subTest(content_length=len(content)):
                blob.write_bytes(content)
                self.mutate_manifest(shelf, lambda data: data["entries"][0].update(stored_size=len(content)))
                with self.assertRaisesRegex(ShelfError, "damaged"):
                    self.manager.restore(shelf["id"])
                self.assertEqual((self.wc / "large").read_bytes(), b"base")
                self.assertTrue(self.manifest_path(shelf).exists())

    def test_expansion_stops_at_declared_size_without_replacing_destination(self):
        self.manager.node("bomb", "modified", content=b"A" * (2 * 1024 * 1024))
        shelf = self.manager.create("bounded expansion")
        self.manager.clean()
        self.mutate_manifest(shelf, lambda data: data["entries"][0].update(size=8192))
        with self.assertRaisesRegex(ShelfError, "expanded size exceeds"):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "bomb").read_bytes(), b"base")
        self.assertEqual(list(self.wc.glob(".svn-restore-*")), [])

    def test_manifest_size_limits_are_checked_before_decompression(self):
        self.manager.node("large", "modified", content=b"A" * 4096)
        shelf = self.manager.create("size limits")
        with patch("svn_client.shelves._MAX_FILE_BYTES", 1024), \
             patch.object(self.manager, "_copy_verified_blob", side_effect=AssertionError("must not decompress")):
            with self.assertRaisesRegex(ShelfError, "size limits"):
                self.manager.details(shelf["id"])
        with patch("svn_client.shelves._MAX_SHELF_BYTES", 1024), \
             patch.object(self.manager, "_copy_verified_blob", side_effect=AssertionError("must not decompress")):
            with self.assertRaisesRegex(ShelfError, "size limits"):
                self.manager.details(shelf["id"])

    def test_create_size_limits_preserve_sources_and_existing_shelves(self):
        shelf = self.simple()
        with patch("svn_client.shelves._MAX_FILE_BYTES", 1):
            with self.assertRaisesRegex(ShelfError, "limit"):
                self.manager.create("too large")
        with patch("svn_client.shelves._MAX_SHELF_BYTES", 1):
            with self.assertRaisesRegex(ShelfError, "limit"):
                self.manager.preflight_create()
        self.assertEqual(self.manager.list()[0]["id"], shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"changed\x00\xff\r\n")
        self.assertEqual(list(self.store.glob(".creating-*")), [])

    def test_invalid_encoding_and_inconsistent_duplicate_metadata_are_rejected(self):
        shelf = self.simple()
        original = self.manifest_path(shelf).read_text()
        for update in ({"blob_encoding": "unknown"}, {"stored_size": True},
                       {"stored_size": -1}, {"stored_size": 0}, {"size": True}):
            with self.subTest(update=update):
                self.manifest_path(shelf).write_text(original)
                self.mutate_manifest(shelf, lambda data: data["entries"][0].update(update))
                with self.assertRaises(ShelfError):
                    self.manager.details(shelf["id"])
        self.manifest_path(shelf).write_text(original)
        def duplicate(data):
            extra = copy.deepcopy(data["entries"][0])
            extra.update(path="second", size=extra["size"] + 1, stored_size=extra["stored_size"] + 1)
            data["entries"].append(extra)
        self.mutate_manifest(shelf, duplicate)
        with self.assertRaisesRegex(ShelfError, "Inconsistent duplicate"):
            self.manager.details(shelf["id"])

    def test_compressed_blob_changed_after_preflight_preserves_destination(self):
        self.manager.node("large", "modified", content=b"A" * 4096)
        shelf = self.manager.create("concurrent blob damage")
        self.manager.clean()
        original = self.manager._preflight
        def preflight(manifest):
            result = original(manifest)
            blob = self.store / shelf["id"] / "blobs" / manifest["entries"][0]["blob"]
            blob.write_bytes(b"corruption")
            return result
        with patch.object(self.manager, "_preflight", side_effect=preflight):
            with self.assertRaisesRegex(ShelfError, "damaged"):
                self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "large").read_bytes(), b"base")
        self.assertEqual(list(self.wc.glob(".svn-restore-*")), [])
        self.assertTrue(self.manifest_path(shelf).exists())

    def test_failed_atomic_publish_does_not_remove_existing_shelves(self):
        shelf = self.simple()
        with patch("svn_client.shelves.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.manager.create("cannot publish")
        self.assertEqual([item["id"] for item in self.manager.list()], [shelf["id"]])
        self.assertEqual(list(self.store.glob(".creating-*")), [])
        self.assertFalse((self.store / ".operation.lock").exists())
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"changed\x00\xff\r\n")

    def test_other_manager_operation_and_abandoned_temporary_are_not_removed(self):
        shelf = self.simple()
        abandoned = self.store / ".creating-abandoned"
        abandoned.mkdir()
        (abandoned / "recoverable").write_bytes(b"keep me")
        other = FakeManager(self.wc, self.store)
        with self.manager._locked():
            with self.assertRaisesRegex(ShelfError, "Another shelf operation"):
                other.delete(shelf["id"])
            self.assertTrue((self.store / ".operation.lock").exists())
        self.manager.create("another shelf")
        self.assertEqual((abandoned / "recoverable").read_bytes(), b"keep me")
        self.assertEqual(len(self.manager.list()), 2)

    def test_lossless_roundtrip_and_backup_never_reverts(self):
        m = self.manager
        m.node("binary.bin", "modified", content=b"\x00\xff\xfe\r\n", props={"custom:binary": enc(b"\x00\xff\r\n")})
        m.node("removed.txt", "deleted", base=b"old deleted content")
        m.node("new", "added", kind="dir", props={"custom:dir": enc(b"value")})
        m.node("new/added.txt", "added", content=b"new text")
        m.node("unversioned", "unversioned", kind="dir")
        (self.wc / "unversioned/empty").mkdir()
        (self.wc / "unversioned/raw.bin").write_bytes(b"\x00raw\xff")
        m.node("propertydir", "normal", kind="dir", props={"custom:prop": enc(b"props only")})
        before = m._status()
        shelf = m.create("Complete backup")
        self.assertEqual(before, m._status())
        self.assertEqual((self.wc / "binary.bin").read_bytes(), b"\x00\xff\xfe\r\n")
        self.assertFalse((self.wc / "removed.txt").exists())
        self.assertEqual(shelf["entry_count"], 8)
        m.clean()
        result = m.restore(shelf["id"])
        self.assertTrue(result["shelf_retained"])
        self.assertEqual((self.wc / "binary.bin").read_bytes(), b"\x00\xff\xfe\r\n")
        self.assertFalse((self.wc / "removed.txt").exists())
        self.assertEqual((self.wc / "new/added.txt").read_bytes(), b"new text")
        self.assertTrue((self.wc / "unversioned/empty").is_dir())
        self.assertEqual((self.wc / "unversioned/raw.bin").read_bytes(), b"\x00raw\xff")
        self.assertEqual(m.current_props["binary.bin"]["custom:binary"], enc(b"\x00\xff\r\n"))
        self.assertEqual(m.current_props["propertydir"]["custom:prop"], enc(b"props only"))
        self.assertEqual(m.list()[0]["id"], shelf["id"])
        self.assertEqual(len(m.details(shelf["id"])["entries"]), 8)

    def test_root_directory_properties_and_property_removal(self):
        self.manager.current_props["."] = {"custom:root": enc(b"root setting")}
        self.manager.node("props.txt", "normal", props={}, base_props={"custom:removed": enc(b"old")})
        shelf = self.manager.create("props")
        self.manager.clean()
        self.manager.restore(shelf["id"])
        self.assertEqual(self.manager.current_props["."]["custom:root"], enc(b"root setting"))
        self.assertEqual(self.manager.current_props["props.txt"], {})

    def test_restore_replaces_hardlink_without_modifying_other_link(self):
        shelf = self.simple()
        self.manager.clean()
        other_link = self.root / "other-link"
        os.link(self.wc / "file.bin", other_link)
        self.manager.restore(shelf["id"])
        self.assertEqual(other_link.read_bytes(), b"base")
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"changed\x00\xff\r\n")

    def test_base_export_uses_pristine_local_revision_and_unique_destination(self):
        manager = ShelfManager(self.wc, self.store)
        manager._ensure_storage()
        calls = []
        def fake_svn(*args):
            calls.append(args)
            Path(args[-1]).write_bytes(b"translated\r\n$Id: test $")
            return b""
        with patch.object(manager, "_svn", side_effect=fake_svn):
            checksum = manager._exported_base_hash("a@b.txt")
        self.assertEqual(checksum, hashlib.sha256(b"translated\r\n$Id: test $").hexdigest())
        self.assertEqual(calls[0][:-1], ("export", "--quiet", "--ignore-externals", "-r", "BASE", "--", "a@b.txt@"))
        self.assertFalse(Path(calls[0][-1]).exists())

    def test_preflight_create_is_read_only_and_returns_complete_plan(self):
        self.manager.node("file.bin", "modified", content=b"changed\x00\xff")
        self.manager.node("new", "unversioned", kind="dir")
        (self.wc / "new/empty").mkdir()
        before = self.manager._status()
        result = self.manager.preflight_create(["file.bin", "new"])
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["can_create"])
        self.assertEqual(result["paths"], ["file.bin", "new", "new/empty"])
        self.assertEqual(result["entry_count"], 3)
        self.assertEqual(result["repository"]["uuid"], "test-uuid")
        self.assertTrue(result["warnings"])
        self.assertFalse(self.store.exists())
        self.assertEqual(before, self.manager._status())
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"changed\x00\xff")
        self.assertEqual(self.manager.commands, [])

    def test_preflight_create_unsupported_does_not_create_storage(self):
        self.manager.node("deleted", "deleted", kind="dir")
        with self.assertRaisesRegex(ShelfError, "Deleted directories"):
            self.manager.preflight_create(["deleted"])
        self.assertFalse(self.store.exists())
        self.assertEqual(self.manager.commands, [])

    def test_preflight_create_rejects_unsafe_case_collision(self):
        self.manager.node("UPPER", "unversioned", content=b"a")
        self.manager.node("upper", "unversioned", content=b"b")
        with self.assertRaisesRegex(ShelfError, "case-colliding"):
            self.manager.preflight_create(["UPPER", "upper"])
        self.assertFalse(self.store.exists())

    def test_preflight_restore_does_not_mutate_shelf_or_worktree(self):
        shelf = self.simple()
        self.manager.clean()
        before = {str(p.relative_to(self.store)): p.read_bytes()
                  for p in self.store.rglob("*") if p.is_file()}
        result = self.manager.preflight_restore(shelf["id"])
        after = {str(p.relative_to(self.store)): p.read_bytes()
                 for p in self.store.rglob("*") if p.is_file()}
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["can_restore"])
        self.assertTrue(result["shelf_retained"])
        self.assertEqual(result["paths"], ["file.bin"])
        self.assertEqual(before, after)
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"base")
        self.assertEqual(self.manager.commands, [])
        self.assertFalse((self.store / ".operation.lock").exists())

    def test_preflight_restore_then_edit_does_not_authorize_stale_restore(self):
        shelf = self.simple()
        self.manager.clean()
        self.manager.preflight_restore(shelf["id"])
        (self.wc / "file.bin").write_bytes(b"later edit")
        with self.assertRaises(ShelfConflict):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"later edit")

    def test_preflight_restore_conflict_and_corruption_fail_without_writes(self):
        shelf = self.simple()
        with self.assertRaises(ShelfConflict):
            self.manager.preflight_restore(shelf["id"])
        self.manager.clean()
        blob = next((self.store / shelf["id"] / "blobs").iterdir())
        blob.write_bytes(b"corruption")
        with self.assertRaisesRegex(ShelfError, "damaged"):
            self.manager.preflight_restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"base")
        self.assertEqual(self.manager.commands, [])
        self.assertFalse((self.store / ".operation.lock").exists())

    def test_readonly_methods_never_create_missing_storage(self):
        self.assertEqual(self.manager.list(), [])
        with self.assertRaises(ShelfError):
            self.manager.details("0" * 32)
        with self.assertRaises(ShelfError):
            self.manager.preflight_restore("0" * 32)
        self.assertFalse(self.store.exists())

    def test_readonly_methods_do_not_call_storage_mutation_helpers(self):
        shelf = self.simple()
        self.manager.clean()
        with patch.object(self.manager, "_ensure_storage", side_effect=AssertionError("unexpected storage creation")), patch.object(self.manager, "_storage_lock", side_effect=AssertionError("unexpected lock creation")):
            self.assertTrue(self.manager.list())
            self.assertEqual(self.manager.details(shelf["id"])["id"], shelf["id"])
            self.assertTrue(self.manager.preflight_restore(shelf["id"])["can_restore"])
            self.manager.states["file.bin"]["item"] = "modified"
            (self.wc / "file.bin").write_bytes(b"new edit")
            self.assertTrue(self.manager.preflight_create(["file.bin"])["can_create"])

    def test_preflights_refuse_active_operation_without_deleting_lock(self):
        self.manager.node("file.txt", "modified", content=b"edit")
        self.store.mkdir()
        lock = self.store / ".operation.lock"
        lock.write_text("12345")
        for action in (lambda: self.manager.preflight_create(["file.txt"]),
                       lambda: self.manager.preflight_restore("0" * 32)):
            with self.assertRaisesRegex(ShelfError, "Another shelf operation"):
                action()
        self.assertEqual(lock.read_text(), "12345")

    def test_base_export_refuses_temp_inside_working_copy(self):
        manager = ShelfManager(self.wc, self.store)
        with patch("svn_client.shelves.tempfile.gettempdir", return_value=str(self.wc)), patch.object(manager, "_svn") as svn:
            with self.assertRaisesRegex(ShelfError, "temporary storage must be outside"):
                manager._exported_base_hash("file.txt")
        svn.assert_not_called()
        self.assertEqual(list(self.wc.iterdir()), [])
        self.assertFalse(self.store.exists())

    def test_all_paths_preflight_before_any_write(self):
        self.manager.node("a.txt", "modified", content=b"A")
        self.manager.node("z.txt", "modified", content=b"Z")
        shelf = self.manager.create("two files")
        self.manager.clean()
        self.manager.states["z.txt"]["item"] = "modified"
        (self.wc / "z.txt").write_bytes(b"other work")
        with self.assertRaises(ShelfConflict):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "a.txt").read_bytes(), b"base")
        self.assertEqual((self.wc / "z.txt").read_bytes(), b"other work")
        self.assertEqual(self.manager.commands, [])

    def test_unversioned_collision_even_identical_is_rejected(self):
        self.manager.node("new.txt", "unversioned", content=b"new")
        shelf = self.manager.create("unversioned")
        with self.assertRaises(ShelfConflict):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "new.txt").read_bytes(), b"new")

    def test_changed_base_revision_rejected(self):
        shelf = self.simple()
        self.manager.clean()
        self.manager.revision = "2"
        with self.assertRaisesRegex(ShelfConflict, "BASE"):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"base")

    def test_dirty_bytes_hidden_by_clean_status_are_rejected(self):
        shelf = self.simple()
        self.manager.clean()
        (self.wc / "file.bin").write_bytes(b"same-size-hidden-edit")
        # Fake SVN still reports normal, as a size/mtime heuristic can do.
        with self.assertRaisesRegex(ShelfConflict, "differ from translated BASE"):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"same-size-hidden-edit")

    def test_selected_added_child_requires_parent(self):
        self.manager.node("new", "added", kind="dir")
        self.manager.node("new/child", "added", content=b"child")
        with self.assertRaisesRegex(ShelfError, "parent directory"):
            self.manager.create("incomplete", ["new/child"])
        self.assertEqual(self.manager.list(), [])

    def test_wrong_repository_rejected(self):
        shelf = self.simple()
        self.manager.clean()
        self.manager.repo_url += "/other-branch"
        with self.assertRaisesRegex(ShelfConflict, "different repository"):
            self.manager.restore(shelf["id"])

    def test_blob_corruption_rejected_before_writing(self):
        shelf = self.simple()
        self.manager.clean()
        blob = next((self.store / shelf["id"] / "blobs").iterdir())
        blob.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ShelfError, "damaged"):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"base")

    def test_traversal_and_windows_unsafe_paths(self):
        for path in ("../outside", "/etc/passwd", "C:/file", "C:\\file", "\\\\server\\share", "a/../b", "a//b", "./a", "a/.svn/file", "NUL.txt", "a:stream", "LPT¹", "CONIN$", "trailing.", "trailing "):
            with self.subTest(path=path), self.assertRaises(ShelfError):
                _safe_relative(path)
        shelf = self.simple()
        self.manager.clean()
        self.mutate_manifest(shelf, lambda data: data["entries"][0].update(path="../escape"))
        with self.assertRaises(ShelfError):
            self.manager.restore(shelf["id"])
        self.assertFalse((self.root / "escape").exists())

    def test_case_collision_rejected_before_writing(self):
        shelf = self.simple()
        self.manager.clean()
        def duplicate(data):
            extra = copy.deepcopy(data["entries"][0])
            extra["path"] = "FILE.BIN"
            data["entries"].append(extra)
        self.mutate_manifest(shelf, duplicate)
        with self.assertRaisesRegex(ShelfError, "case-colliding"):
            self.manager.restore(shelf["id"])

    def test_unsupported_states_leave_no_shelf(self):
        for flag in ({"item": "replaced"}, {"item": "conflicted"}, {"item": "missing"}, {"copied": "true"}, {"switched": "true"}, {"tree-conflicted": "true"}, {"file-external": "true"}, {"wc-locked": "true"}, {"moved-from": "other.txt"}):
            with self.subTest(flag=flag):
                self.manager.node("file.txt", "modified", content=b"new")
                self.manager.states["file.txt"].update(flag)
                with self.assertRaises(ShelfError):
                    self.manager.create("unsupported")
                self.assertEqual(self.manager.list(), [])
                self.assertEqual((self.wc / "file.txt").read_bytes(), b"new")

    def test_deleted_directory_rejected(self):
        self.manager.node("deleted", "deleted", kind="dir")
        with self.assertRaisesRegex(ShelfError, "Deleted directories"):
            self.manager.create("unsupported")

    def test_deleted_keep_local_content_rejected(self):
        self.manager.node("kept.txt", "deleted")
        (self.wc / "kept.txt").write_bytes(b"do not lose this")
        with self.assertRaisesRegex(ShelfError, "still contains local data"):
            self.manager.create("unsupported")
        self.assertEqual((self.wc / "kept.txt").read_bytes(), b"do not lose this")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_target_and_ancestor_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"private")
        (self.wc / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ShelfError, "Symlink"):
            self.manager._path("link/file")
        self.manager.states["link"] = {"item": "unversioned", "kind": "dir"}
        with self.assertRaisesRegex(ShelfError, "Symlink"):
            self.manager.create("unsafe")

    def test_nested_working_copy_ancestor_rejected(self):
        nested = self.wc / "nested"
        (nested / ".svn").mkdir(parents=True)
        (nested / "file").write_bytes(b"data")
        with self.assertRaisesRegex(ShelfError, "Nested"):
            self.manager._path("nested/file")

    def test_source_change_during_backup_aborts(self):
        self.manager.node("file.txt", "modified", content=b"original edit")
        original = self.manager._status
        calls = 0
        def status(paths=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.wc / "file.txt").write_bytes(b"newer edit")
            return original(paths)
        self.manager._status = status
        with self.assertRaisesRegex(ShelfError, "File changed"):
            self.manager.create("unstable")
        self.assertEqual(self.manager.list(), [])
        self.assertEqual((self.wc / "file.txt").read_bytes(), b"newer edit")

    def test_partial_failure_retains_shelf_and_reports_paths(self):
        shelf = self.simple()
        self.manager.clean()
        with patch.object(self.manager, "_set_properties", side_effect=ShelfError("disk failure")):
            with self.assertRaisesRegex(ShelfError, "after changing or attempting file.bin.*Shelf retained"):
                self.manager.restore(shelf["id"])
        self.assertTrue(self.manifest_path(shelf).is_file())
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"changed\x00\xff\r\n")

    def test_restore_observation_detects_concurrent_edit(self):
        shelf = self.simple()
        self.manager.clean()
        original = self.manager._preflight
        def preflight(manifest):
            result = original(manifest)
            (self.wc / "file.bin").write_bytes(b"concurrent edit")
            return result
        self.manager._preflight = preflight
        with self.assertRaisesRegex(ShelfConflict, "during restore"):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"concurrent edit")

    def test_delete_only_removes_backup(self):
        shelf = self.simple()
        self.manager.delete(shelf["id"])
        self.assertEqual(self.manager.list(), [])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"changed\x00\xff\r\n")
        with self.assertRaises(ShelfError):
            self.manager.delete("../wc")

    def test_store_inside_working_copy_rejected(self):
        with self.assertRaisesRegex(ShelfError, "outside"):
            ShelfManager(self.wc, self.wc / "backups")

    def test_binary_xml_property_values(self):
        manager = ShelfManager(self.wc, self.store)
        xml = b'<properties><target path="f"><property name="binary" encoding="base64">AP8NCg==</property><property name="text">a&#13;\nb</property></target></properties>'
        with patch.object(manager, "_svn", return_value=xml):
            props = manager._properties("f")
        self.assertEqual(base64.b64decode(props["binary"]), b"\x00\xff\r\n")
        self.assertEqual(base64.b64decode(props["text"]), b"a\r\nb")

    def test_status_detects_file_external_and_lock(self):
        manager = ShelfManager(self.wc, self.store)
        xml = b'<status><target path="."><entry path="f"><wc-status item="modified" props="none" file-external="true" wc-locked="true" /></entry></target></status>'
        with patch.object(manager, "_svn", return_value=xml):
            status = manager._status()
        with self.assertRaises(ShelfError):
            manager._check_status(status[0])

    def test_raw_runner_is_noninteractive_and_does_not_shell_parse(self):
        manager = ShelfManager(self.wc, self.store)
        with patch("svn_client.backend.discover_svn", return_value="svn"), patch("svn_client.shelves.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"\x00\xff", b"")) as run:
            self.assertEqual(manager._svn("cat", "--", manager._target("a@b")), b"\x00\xff")
        command = run.call_args.args[0]
        self.assertEqual(command, ["svn", "--non-interactive", "--no-auth-cache", "cat", "--", "a@b@"])
        self.assertNotEqual(run.call_args.kwargs.get("shell"), True)
        self.assertIn("timeout", run.call_args.kwargs)


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "svn and svnadmin are not installed")
class RealSvnShelfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.wc = self.root / "repo", self.root / "wc"
        subprocess.run(["svnadmin", "create", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["svn", "checkout", self.repo.as_uri(), str(self.wc)], check=True, capture_output=True)
        self.svn("mkdir", "dir")
        (self.wc / "file.bin").write_bytes(b"base\x00\xff")
        (self.wc / "delete.txt").write_bytes(b"delete later")
        self.svn("add", "file.bin", "delete.txt")
        self.svn("commit", "-m", "initial")
        self.svn("update")
        self.manager = ShelfManager(self.wc, self.root / "shelves")

    def tearDown(self):
        self.temp.cleanup()

    def svn(self, *args):
        return subprocess.run(["svn", "--non-interactive", *args], cwd=self.wc, check=True, capture_output=True).stdout

    def test_real_binary_properties_add_delete_and_unversioned_roundtrip(self):
        (self.wc / "file.bin").write_bytes(b"edit\x00\xfe\r\n")
        prop = self.root / "prop.bin"
        prop.write_bytes(b"\x00\xff\r\n")
        self.svn("propset", "custom:binary", "--file", str(prop), "file.bin")
        self.svn("propset", "custom:dir", "value", "dir")
        self.svn("delete", "delete.txt")
        self.svn("mkdir", "added")
        (self.wc / "added/new.txt").write_bytes(b"new")
        self.svn("add", "added/new.txt")
        (self.wc / "loose/empty").mkdir(parents=True)
        (self.wc / "loose/data.bin").write_bytes(b"loose\x00\xff")
        shelf = self.manager.create("real integration")
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"edit\x00\xfe\r\n")
        self.svn("revert", "-R", ".")
        shutil.rmtree(self.wc / "added")
        shutil.rmtree(self.wc / "loose")
        self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"edit\x00\xfe\r\n")
        self.assertEqual(self.svn("propget", "--strict", "custom:binary", "file.bin"), b"\x00\xff\r\n")
        self.assertEqual(self.svn("propget", "--strict", "custom:dir", "dir"), b"value")
        self.assertFalse((self.wc / "delete.txt").exists())
        self.assertEqual((self.wc / "added/new.txt").read_bytes(), b"new")
        self.assertTrue((self.wc / "loose/empty").is_dir())
        self.assertEqual((self.wc / "loose/data.bin").read_bytes(), b"loose\x00\xff")
        self.assertTrue(self.manager.list())

    def test_real_crlf_and_keyword_translation_roundtrip(self):
        path = self.wc / "translated.txt"
        path.write_bytes(b"$Id$\r\nline one\r\n")
        self.svn("add", "translated.txt")
        self.svn("propset", "svn:eol-style", "CRLF", "translated.txt")
        self.svn("propset", "svn:keywords", "Id", "translated.txt")
        self.svn("commit", "-m", "translated file")
        self.svn("update")
        path.write_bytes(path.read_bytes().replace(b"line one", b"line two"))
        expected = path.read_bytes()
        shelf = self.manager.create("translated")
        self.svn("revert", "translated.txt")
        self.manager.restore(shelf["id"])
        self.assertEqual(path.read_bytes(), expected)

    def test_real_compressed_binary_and_property_roundtrip(self):
        content = bytes(range(256)) * 8192
        (self.wc / "file.bin").write_bytes(content)
        prop = self.root / "prop.bin"
        prop.write_bytes(b"\x00\xff\r\n")
        self.svn("propset", "custom:binary", "--file", str(prop), "file.bin")
        shelf = self.manager.create("compressed real integration")
        entry = self.manager.details(shelf["id"])["entries"][0]
        self.assertEqual(entry["blob_encoding"], "zlib")
        self.svn("revert", "file.bin")
        self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), content)
        self.assertEqual(self.svn("propget", "--strict", "custom:binary", "file.bin"), b"\x00\xff\r\n")

    def test_real_local_conflict_refuses_entire_restore(self):
        (self.wc / "file.bin").write_bytes(b"edit")
        shelf = self.manager.create("conflict")
        (self.wc / "file.bin").write_bytes(b"other edit")
        with self.assertRaises(ShelfConflict):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"other edit")


if __name__ == "__main__":
    unittest.main()

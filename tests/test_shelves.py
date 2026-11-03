"""Shelf safety tests; real SVN round trips run when svn/svnadmin are installed."""
from __future__ import annotations

import base64
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
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

    def _infos(self, paths):
        return {path: self._info(path) for path in paths}

    def _properties_many(self, paths, base=False):
        return {path: self._properties(path, base=base) for path in paths}

    def _status(self, paths=None):
        result = []
        for path, node in sorted(self.states.items()):
            if paths and not any(p == "." or path == p or path.startswith(p + "/") for p in paths):
                continue
            props = "modified" if self.current_props.get(path, {}) != self.base_props.get(path, {}) else "none"
            result.append({"path": path, "item": node["item"], "props": props,
                           **{k: v for k, v in node.items() if k not in ("item", "kind")}})
        return result

    def _base(self, path, info=None, properties=None):
        info = info or self._info(path)
        result = {k: info[k] for k in ("kind", "revision", "url", "uuid")}
        result["properties"] = self._properties(path, base=True) if properties is None else properties
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


class XmlManager(FakeManager):
    """Use production parsers/commands, substituting only deterministic SVN I/O."""
    _info = ShelfManager._info
    _infos = ShelfManager._infos
    _properties = ShelfManager._properties
    _properties_many = ShelfManager._properties_many
    _base = ShelfManager._base
    _status = ShelfManager._status

    def _svn(self, *args):
        command = args[0]
        if command not in ("info", "status", "proplist", "cat", "export"):
            return super()._svn(*args)
        self.commands.append(args)
        targets = [value[:-1] for value in args[args.index("--") + 1:]]
        if command == "info":
            root = ET.Element("info")
            for path in reversed(targets):  # Deliberately differ from argv order.
                info = FakeManager._info(self, path)
                entry = ET.SubElement(root, "entry", path=path, kind=info["kind"], revision=info["revision"])
                ET.SubElement(entry, "url").text = info["url"]
                repo = ET.SubElement(entry, "repository")
                ET.SubElement(repo, "uuid").text = info["uuid"]
                wc = ET.SubElement(entry, "wc-info")
                ET.SubElement(wc, "schedule").text = info["schedule"]
                ET.SubElement(wc, "depth").text = info["depth"]
            return ET.tostring(root)
        if command == "status":
            root = ET.Element("status")
            target = ET.SubElement(root, "target", path=".")
            for state in FakeManager._status(self, targets):
                entry = ET.SubElement(target, "entry", path=state["path"])
                attrs = {key: value for key, value in state.items() if key != "path" and value is not None}
                attrs["revision"] = self.revision
                ET.SubElement(entry, "wc-status", attrs)
            return ET.tostring(root)
        if command == "proplist":
            root = ET.Element("properties")
            for path in reversed(targets):
                props = FakeManager._properties(self, path, base="BASE" in args)
                if props:  # Real SVN omits empty-property targets entirely.
                    target = ET.SubElement(root, "target", path=path)
                    for name, value in props.items():
                        ET.SubElement(target, "property", name=name, encoding="base64").text = value
            return ET.tostring(root)
        if command == "cat":
            return self.base_data[targets[0]]
        if command == "export":
            Path(args[-1]).write_bytes(self.base_data[args[-2][:-1]])
            return b""
        raise AssertionError(command)


class UnbatchedXmlManager(XmlManager):
    """Previous one-command-per-target strategy for reproducible count comparison."""
    def _target_batches(self, paths):
        for path in paths:
            yield [self._relative(path)]


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

    def test_batch_metadata_reduces_100_file_create_from_603_to_207_calls(self):
        manifests, counts = [], []
        for index, manager_type in enumerate((UnbatchedXmlManager, XmlManager)):
            wc = self.root / f"wc-{index}"
            wc.mkdir()
            manager = manager_type(wc, self.root / f"store-{index}")
            for i in range(100):
                manager.node(f"file {i}@.bin", "modified", content=(f"modified {i}\r\n".encode() * 300),
                             base=(f"base {i}\r\n".encode() * 300),
                             props={"custom:binary": enc(bytes([i, 0, 255]))} if i % 2 else {},
                             base_props={"custom:base": enc(b"\x00\xff")} if i % 3 else {})
            with patch("svn_client.shelves.subprocess.run", side_effect=AssertionError("no real SVN")):
                shelf = manager.create("batch count comparison")
                counts.append(Counter(args[0] for args in manager.commands))
                manifests.append(manager.details(shelf["id"])["entries"])
                manager.clean()
                manager.restore(shelf["id"])
            for i in range(100):
                self.assertEqual((wc / f"file {i}@.bin").read_bytes(), f"modified {i}\r\n".encode() * 300)
                self.assertEqual(manager.current_props[f"file {i}@.bin"],
                                 {"custom:binary": enc(bytes([i, 0, 255]))} if i % 2 else {})
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(counts[0], {"info": 101, "status": 2, "proplist": 300, "cat": 100, "export": 100})
        self.assertEqual(counts[1], {"info": 2, "status": 2, "proplist": 3, "cat": 100, "export": 100})
        self.assertEqual(sum(counts[0].values()), 603)
        self.assertEqual(sum(counts[1].values()), 207)

    def test_preflight_uses_fresh_batches_and_never_reuses_previous_results(self):
        manager = XmlManager(self.wc, self.store)
        manager.node("a@b", "modified", content=b"changed", props={"binary": enc(b"\x00\xff")})
        first = manager.preflight_create()
        first_calls = Counter(args[0] for args in manager.commands)
        manager.current_props["a@b"] = {"binary": enc(b"new value")}
        manager.commands.clear()
        second = manager.preflight_create()
        self.assertEqual(Counter(args[0] for args in manager.commands), first_calls)
        self.assertNotEqual(first["entries"][0]["properties"], second["entries"][0]["properties"])
        self.assertFalse(self.store.exists())

    def test_batch_property_recheck_detects_change_with_same_status_flags(self):
        manager = XmlManager(self.wc, self.store)
        manager.node("file", "modified", content=b"edit", props={"value": enc(b"one")})
        original = manager._capture_file
        def capture(path, blobs=None):
            result = original(path, blobs)
            manager.current_props["file"] = {"value": enc(b"two")}
            return result
        with patch.object(manager, "_capture_file", side_effect=capture):
            with self.assertRaisesRegex(ShelfError, "Properties changed"):
                manager.create("unstable properties")
        self.assertEqual(manager.list(), [])
        self.assertEqual((self.wc / "file").read_bytes(), b"edit")

    def test_batched_mixed_root_added_deleted_and_unversioned_roundtrip(self):
        manager = XmlManager(self.wc, self.store)
        manager.current_props["."] = {"root": enc(b"\x00\xff")}
        manager.node("deleted", "deleted", base=b"removed")
        manager.node("new", "added", kind="dir", props={"dir": enc(b"new directory")})
        manager.node("new/child@name", "added", content=b"added\x00\xff")
        manager.node("loose", "unversioned", content=b"loose\x00")
        shelf = manager.create("mixed batches")
        manager.clean()
        manager.restore(shelf["id"])
        self.assertEqual(manager.current_props["."], {"root": enc(b"\x00\xff")})
        self.assertFalse((self.wc / "deleted").exists())
        self.assertEqual((self.wc / "new/child@name").read_bytes(), b"added\x00\xff")
        self.assertEqual((self.wc / "loose").read_bytes(), b"loose\x00")
        self.assertEqual(manager.current_props["new"], {"dir": enc(b"new directory")})

    def test_failed_later_metadata_batch_does_not_publish_or_change_sources(self):
        manager = XmlManager(self.wc, self.store)
        for name in ("a", "b", "c"):
            manager.node(name, "modified", content=name.encode())
        original = manager._svn
        def svn(*args):
            if args[0] == "proplist" and args[-1] == "c@":
                raise ShelfError("synthetic later-batch failure")
            return original(*args)
        with patch("svn_client.shelves._SVN_BATCH_TARGETS", 2), patch.object(manager, "_svn", side_effect=svn):
            with self.assertRaisesRegex(ShelfError, "later-batch failure"):
                manager.create("no partial batch")
        self.assertEqual(manager.list(), [])
        self.assertEqual(list(self.store.glob(".creating-*")), [])
        self.assertFalse((self.store / ".operation.lock").exists())
        for name in ("a", "b", "c"):
            self.assertEqual((self.wc / name).read_bytes(), name.encode())

    def test_info_batches_reject_missing_extra_duplicate_or_copied_targets(self):
        manager = ShelfManager(self.wc, self.store)
        valid = '<entry path="a" kind="file" revision="1"><url>file:///repo/a</url><repository><uuid>id</uuid></repository></entry>'
        cases = ("<info/>", "<wrong/>", "<info>" + valid * 2 + "</info>",
                 "<info>" + valid.replace('path="a"', 'path="other"') + "</info>",
                 "<info>" + valid.replace("</entry>", "<wc-info><copy-from-url>file:///copy</copy-from-url></wc-info></entry>") + "</info>")
        for xml in cases:
            with self.subTest(xml=xml), patch.object(manager, "_svn", return_value=xml.encode()):
                with self.assertRaises(ShelfError):
                    manager._infos(["a"])

    def test_property_batches_handle_empty_targets_binary_values_and_absolute_paths(self):
        manager = ShelfManager(self.wc, self.store)
        root = ET.Element("properties")
        target = ET.SubElement(root, "target", path=str(self.wc / "a@b"))
        ET.SubElement(target, "property", name="binary", encoding="base64").text = " AP8N\nCg== "
        ET.SubElement(target, "property", name="text").text = "a\r\nb"
        with patch.object(manager, "_svn", return_value=ET.tostring(root).replace(b"\r", b"&#13;")) as svn:
            result = manager._properties_many(["empty", "a@b"], base=True)
        self.assertEqual(result, {"empty": {}, "a@b": {"binary": enc(b"\x00\xff\r\n"), "text": enc(b"a\r\nb")}})
        self.assertEqual(svn.call_args.args, ("proplist", "--xml", "--verbose", "--depth", "empty", "-r", "BASE", "--", "empty@", "a@b@"))

    def test_property_batches_reject_extra_duplicate_or_invalid_xml_fields(self):
        manager = ShelfManager(self.wc, self.store)
        target = '<target path="a"><property name="x">v</property></target>'
        cases = ("<wrong/>", "<properties>" + target * 2 + "</properties>",
                 '<properties><target path="unexpected"/></properties>',
                 '<properties><target path="../escape"/></properties>',
                 '<properties><target path="a"><property name="x">one</property><property name="x">two</property></target></properties>',
                 '<properties><target path="a"><property name="x" encoding="unknown">a</property></target></properties>',
                 '<properties><target path="a"><property name="svn:special">link</property></target></properties>',
                 '<properties><target path="a"><property name="x"><nested/></property></target></properties>')
        for xml in cases:
            with self.subTest(xml=xml), patch.object(manager, "_svn", return_value=xml.encode()):
                with self.assertRaises(ShelfError):
                    manager._properties_many(["a"])

    def test_nonzero_batch_exit_rejects_well_formed_partial_xml(self):
        manager = ShelfManager(self.wc, self.store)
        result = subprocess.CompletedProcess([], 1, b'<properties><target path="a"/></properties>', b"path b not found")
        with patch("svn_client.backend.discover_svn", return_value="svn"), \
             patch("svn_client.shelves.subprocess.run", return_value=result):
            with self.assertRaisesRegex(ShelfError, "path b not found"):
                manager._properties_many(["a", "b"])

    def test_batch_bounds_cover_every_target_without_recursive_scope(self):
        manager = ShelfManager(self.wc, self.store)
        paths = [f"{i}-" + "文" * 60 for i in range(300)]
        batches = list(manager._target_batches(paths))
        self.assertEqual([path for batch in batches for path in batch], paths)
        self.assertGreater(len(batches), 3)
        for batch in batches:
            self.assertLessEqual(len(batch), 128)
            self.assertLessEqual(sum(len((path + "@").encode("utf-8")) + 3 for path in batch), 12000)
        with patch.object(manager, "_svn", return_value=b"<properties/>") as svn:
            self.assertEqual(manager._properties_many(paths), {path: {} for path in paths})
        for call in svn.call_args_list:
            self.assertIn("empty", call.args)
            self.assertNotIn("-R", call.args)
            self.assertNotIn(".@", call.args)
        with patch.object(manager, "_svn", side_effect=AssertionError("empty selection must not run SVN")):
            self.assertEqual(manager._infos([]), {})
            self.assertEqual(manager._properties_many([]), {})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_batch_targets_refuse_symlink_ancestors_before_svn(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.wc / "link").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Creating symlinks requires a Windows privilege unavailable in this environment")
            raise
        manager = ShelfManager(self.wc, self.store)
        with patch.object(manager, "_svn", side_effect=AssertionError("unsafe SVN target")):
            with self.assertRaisesRegex(ShelfError, "Symlink"):
                manager._properties_many(["link/file"])

    def test_batch_targets_reject_link_metadata_without_symlink_privilege(self):
        link = self.wc / "link"
        link.mkdir()
        manager = ShelfManager(self.wc, self.store)
        original_lstat = Path.lstat
        for mode, attributes in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFDIR | 0o755, 0x400)):
            def metadata(path, *args, **kwargs):
                if path == link:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                return original_lstat(path, *args, **kwargs)
            for method in (manager._infos, manager._properties_many):
                with self.subTest(mode=mode, attributes=attributes, method=method.__name__):
                    with patch.object(Path, "lstat", metadata), patch.object(manager, "_svn") as svn:
                        with self.assertRaisesRegex(ShelfError, "Symlink"):
                            method(["link/file"])
                        svn.assert_not_called()

    def test_oversized_file_is_refused_before_buffered_base_cat(self):
        manager = XmlManager(self.wc, self.store)
        manager.node("large", "modified", content=b"12345")
        with patch("svn_client.shelves._MAX_FILE_BYTES", 4):
            with self.assertRaisesRegex(ShelfError, "size limits"):
                manager.preflight_create()
        self.assertNotIn("cat", [command[0] for command in manager.commands])
        self.assertFalse(self.store.exists())

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
        try:
            (self.wc / "link").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Creating symlinks requires a Windows privilege unavailable in this environment")
            raise
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

    def test_real_multiple_metadata_batches_and_empty_property_targets(self):
        paths = [f"batch {i}.bin" for i in range(130)]
        for relative in paths:
            (self.wc / relative).write_bytes(b"base\r\n")
        self.svn("add", "--", *paths)
        self.svn("commit", "-m", "batched fixture")
        self.svn("update")
        for relative in paths:
            (self.wc / relative).write_bytes(relative.encode() + b"\x00\xff\r\n")
        prop = self.root / "prop.bin"
        prop.write_bytes(b"\x00\xff\r\n")
        self.svn("propset", "custom:binary", "--file", str(prop), paths[129])
        shelf = self.manager.create("real metadata batches")
        self.assertEqual(shelf["entry_count"], 130)
        self.svn("revert", "-R", ".")
        self.manager.restore(shelf["id"])
        for relative in paths:
            self.assertEqual((self.wc / relative).read_bytes(), relative.encode() + b"\x00\xff\r\n")
        self.assertEqual(self.svn("propget", "--strict", "custom:binary", paths[129]), b"\x00\xff\r\n")

    def test_real_local_conflict_refuses_entire_restore(self):
        (self.wc / "file.bin").write_bytes(b"edit")
        shelf = self.manager.create("conflict")
        (self.wc / "file.bin").write_bytes(b"other edit")
        with self.assertRaises(ShelfConflict):
            self.manager.restore(shelf["id"])
        self.assertEqual((self.wc / "file.bin").read_bytes(), b"other edit")


if __name__ == "__main__":
    unittest.main()

"""Repeatable synthetic measurements. Never opens a user's working copy or SVN.

Run from the project root: python benchmarks/benchmark_client.py --help
Only temporary fixtures and the explicitly named JSON output are written.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))


def source_hashes():
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "svn_client").glob("*.py"))}


IMPORTED_SOURCE_HASHES = source_hashes()
from svn_client.backend import CommandResult, SvnClient  # noqa: E402
from svn_client.shelves import ShelfManager  # noqa: E402
from svn_client.ui import directory_entries, grouped_entries, local_files, read_groupable_status  # noqa: E402

MIB = 1024 * 1024


def require(condition, message):
    """Keep fixture/result verification active even under python -O."""
    if not condition:
        raise AssertionError(message)


def digest_file(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(MIB):
            checksum.update(chunk)
    return checksum.hexdigest()


def measure(work, repeats, *, prepare=lambda: None, cleanup=lambda result: None,
            trace_memory=True):
    """One untimed warmup, untraced timings, then a separate traced run.

    Preparation, cleanup and explicit garbage collection are outside the timer.
    Automatic garbage collection remains enabled during the operation.
    """
    prepare()
    value = work()
    cleanup(value)
    del value
    samples = []
    for _ in range(repeats):
        prepare()
        gc.collect()
        start = time.perf_counter_ns()
        value = work()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
        cleanup(value)
        del value
    result = {
        "samples_ms": [round(value, 3) for value in samples],
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3), "max_ms": round(max(samples), 3),
        "warmup_runs": 1, "timed_runs": repeats,
    }
    if trace_memory:
        prepare()
        gc.collect()
        tracemalloc.start()
        try:
            value = work()
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        cleanup(value)
        del value
        result.update(peak_traced_mib=round(peak / MIB, 3),
                      retained_traced_mib=round(retained / MIB, 3))
    return result


def status_fixture(count):
    """Mixed verbose status, with both clean and changed changelist members."""
    root = ET.Element("status")
    target = ET.SubElement(root, "target", path=".")
    groups = {}
    counts = Counter()
    for i in range(count):
        bucket = i % 100
        item = ("normal" if bucket < 80 else "modified" if bucket < 92 else
                "added" if bucket < 95 else "deleted" if bucket < 97 else
                "missing" if bucket == 97 else "conflicted" if bucket == 98 else "unversioned")
        counts[item] += 1
        parent = target
        if i % 5 == 0 and item != "unversioned":
            name = f"任务 & group {i % 20:02d}"
            if name not in groups:
                groups[name] = ET.SubElement(root, "changelist", name=name)
            parent = groups[name]
        entry = ET.SubElement(parent, "entry", path=f"assets/目录{i // 1000:04d}/file @ {i:07d}.txt")
        node = ET.SubElement(entry, "wc-status", item=item,
                             props="modified" if i % 37 == 0 else "none", revision="100")
        if item == "conflicted":
            node.set("tree-conflicted", "true")
        commit = ET.SubElement(node, "commit", revision="99")
        ET.SubElement(commit, "author").text = "开发者"
    return ET.tostring(root, encoding="unicode"), dict(counts)


def fixture_client(root, xml):
    # Bypass only executable discovery/execution; retain production status parsing.
    client = object.__new__(SvnClient)
    client.working_copy = root
    client.command = lambda args: CommandResult(tuple(args), 0, xml, "")
    return client


def benchmark_status(root, sizes, repeats):
    rows = []
    for count in sizes:
        xml, counts = status_fixture(count)
        client = fixture_client(root, xml)
        parsing = measure(lambda: client.status(verbose=True), repeats)
        entries = client.status(verbose=True)
        require(len(entries) == count, "Status fixture entry count changed")
        row = {"entries": count, "xml_utf8_bytes": len(xml.encode("utf-8")),
               "status_counts": counts, "parse": parsing,
               "group_unfiltered": measure(lambda: grouped_entries(entries), repeats),
               "group_filtered": measure(lambda: grouped_entries(entries, "00000"), repeats)}
        rows.append(row)
        print(f"Status {count:,}: {parsing['median_ms']:.1f} ms, "
              f"{parsing['peak_traced_mib']:.1f} MiB traced peak", flush=True)
    return rows


def benchmark_scan(root, count, repeats):
    root.mkdir()
    for i in range(count):
        folder = root / f"目录{i // 1000:04d}"
        folder.mkdir(exist_ok=True)
        (folder / f"file @ {i:07d}.txt").write_bytes(b"synthetic metadata scan\n")
    # Explicit exclusion sentinels. These are dummy markers, not real SVN metadata.
    (root / ".svn").mkdir()
    (root / ".svn" / "must-not-appear").write_text("fixture", encoding="utf-8")
    nested = root / "nested-working-copy"
    (nested / ".svn").mkdir(parents=True)
    (nested / "must-not-appear").write_text("fixture", encoding="utf-8")
    all_rows, truncated = local_files(root, count + 1)
    require(len(all_rows) == count and not truncated, "Full scan did not return every fixture file")
    require(not any("must-not-appear" in name for name, _ in all_rows), "Excluded directory was scanned")
    result = {"files": count, "file_bytes": 24, "files_per_directory": 1000,
              "excluded_sentinels_verified": True,
              "full_local_files": measure(lambda: local_files(root, count + 1), repeats),
              "capped_local_files": measure(lambda: local_files(root, 3000), repeats),
              "one_directory": measure(lambda: directory_entries(root, root / "目录0000"), repeats)}
    capped, cap_flag = local_files(root, 3000)
    result.update(capped_returned=len(capped), capped_truncated=cap_flag)
    # Current refresh path: real production parser + per-path filesystem checks.
    # Use clean/modified existing files so every status corresponds to real fixture bytes.
    status_root = ET.Element("status")
    target = ET.SubElement(status_root, "target", path=".")
    for path, _ in all_rows:
        entry = ET.SubElement(target, "entry", path=path)
        ET.SubElement(entry, "wc-status", item="modified" if len(target) % 5 == 0 else "normal",
                      props="none", revision="100")
    client = fixture_client(root, ET.tostring(status_root, encoding="unicode"))
    result["refresh_status_and_candidates"] = measure(lambda: read_groupable_status(client, root), repeats)
    pending, candidates = read_groupable_status(client, root)
    require(len(candidates) == count and len(pending) == count // 5, "Refresh candidates changed")
    result.update(refresh_pending=len(pending), refresh_candidates=len(candidates))
    print(f"Scan {count:,}: full {result['full_local_files']['median_ms']:.1f} ms, "
          f"one directory {result['one_directory']['median_ms']:.1f} ms", flush=True)
    return result


class SyntheticShelfManager(ShelfManager):
    """Real shelf algorithm; deterministic in-process stand-in for SVN only.

    No real working copy, external command, server or SVN lock is involved.
    BASE equals literal fixture bytes: EOL/keyword translation is NOT simulated.
    """
    def __init__(self, wc, storage, base):
        super().__init__(wc, storage)
        self.base_directory = base
        self.states = {}
        self.calls = Counter()

    @staticmethod
    def _literal(value):
        return value[:-1] if value.endswith("@") else value

    def _svn(self, *args):
        command = args[0]
        self.calls[command] += 1
        if command == "info":
            root = ET.Element("info")
            targets = [self._literal(value) for value in args[args.index("--") + 1:]]
            for path in targets:
                entry = ET.SubElement(root, "entry", kind="dir" if path == "." else "file",
                                      path=path, revision="1")
                ET.SubElement(entry, "url").text = "file:///synthetic/repository" + ("" if path == "." else "/" + path)
                repository = ET.SubElement(entry, "repository")
                ET.SubElement(repository, "uuid").text = "synthetic-benchmark-repository"
                wc = ET.SubElement(entry, "wc-info")
                ET.SubElement(wc, "schedule").text = "normal"
                ET.SubElement(wc, "depth").text = "infinity"
            return ET.tostring(root)
        if command == "status":
            targets = [self._literal(value) for value in args[args.index("--") + 1:]]
            root = ET.Element("status")
            target = ET.SubElement(root, "target", path=".")
            for path, state in sorted({".": "normal", **self.states}.items()):
                if not any(value == "." or path == value or path.startswith(value + "/") for value in targets):
                    continue
                entry = ET.SubElement(target, "entry", path=path)
                ET.SubElement(entry, "wc-status", item=state, props="none", revision="1")
            return ET.tostring(root)
        if command == "proplist":
            return b"<properties/>"
        if command == "cat":
            return (self.base_directory / self._literal(args[-1])).read_bytes()
        if command == "export":
            shutil.copyfile(self.base_directory / self._literal(args[-2]), args[-1])
            return b""
        raise AssertionError(f"Unexpected synthetic SVN operation: {command}")


def write_pattern(path, size, seed, compressible):
    rng = random.Random(seed)
    block = ((f"synthetic asset {seed:08d}\n".encode() * (MIB // 25 + 1))[:MIB]
             if compressible else rng.randbytes(MIB))
    with path.open("wb") as stream:
        remaining = size
        while remaining:
            piece = block[:remaining]
            stream.write(piece)
            remaining -= len(piece)


def benchmark_shelf(root, name, count, size, repeats, *, versioned, compressible):
    parent = root / name
    wc, base, storage = parent / "wc", parent / "base", parent / "shelves"
    wc.mkdir(parents=True)
    base.mkdir()
    manager = SyntheticShelfManager(wc, storage, base)
    expected = {}
    for i in range(count):
        relative = f"asset {i:06d}.bin"
        write_pattern(wc / relative, size, i + 100, compressible)
        manager.states[relative] = "modified" if versioned else "unversioned"
        expected[relative] = digest_file(wc / relative)
        if versioned:
            write_pattern(base / relative, size, i + 9000, compressible)

    def count_reset():
        manager.calls.clear()

    def remove_shelf(summary):
        shutil.rmtree(storage / summary["id"])

    preflight = measure(manager.preflight_create, repeats, prepare=count_reset)
    preflight_calls = dict(manager.calls)
    creating = measure(lambda: manager.create(name), repeats,
                       prepare=count_reset, cleanup=remove_shelf)
    create_calls = dict(manager.calls)
    summary = manager.create(name)
    shelf_id = summary["id"]
    manifest = json.loads((storage / shelf_id / "manifest.json").read_text(encoding="utf-8"))
    encodings = Counter(entry.get("blob_encoding", "raw") for entry in manifest["entries"])
    stored_bytes = sum(path.stat().st_size for path in (storage / shelf_id / "blobs").iterdir())
    listing = measure(manager.list, repeats, prepare=count_reset)

    def restore_prepare():
        for relative in expected:
            if versioned:
                shutil.copyfile(base / relative, wc / relative)
                manager.states[relative] = "normal"
            else:
                (wc / relative).unlink(missing_ok=True)
                manager.states.pop(relative, None)
        manager.calls.clear()

    restore_preflight = measure(lambda: manager.preflight_restore(shelf_id), repeats,
                                prepare=restore_prepare)
    restore_preflight_calls = dict(manager.calls)

    def verify_restore(result):
        require(result["shelf_retained"], "Restore did not retain its shelf")
        require(len(result["restored_paths"]) == count, "Restore path count changed")
        require(all(digest_file(wc / relative) == checksum for relative, checksum in expected.items()),
                "Restored bytes do not match the original fixture")

    restoring = measure(lambda: manager.restore(shelf_id), repeats,
                        prepare=restore_prepare, cleanup=verify_restore)
    restore_calls = dict(manager.calls)
    result = {"name": name, "files": count, "bytes_per_file": size,
              "input_bytes": count * size, "stored_blob_bytes": stored_bytes,
              "blob_encodings": dict(encodings), "versioned": versioned,
              "compressible": compressible, "restore_hashes_verified": True,
              "create_preflight": preflight, "create": creating, "list_one_shelf": listing,
              "restore_preflight": restore_preflight, "restore": restoring,
              "simulated_svn_calls": {"create_preflight": preflight_calls, "create": create_calls,
                                       "restore_preflight": restore_preflight_calls, "restore": restore_calls}}
    print(f"Shelf {name}: create {creating['median_ms']:.1f} ms, "
          f"restore {restoring['median_ms']:.1f} ms, "
          f"create peak {creating['peak_traced_mib']:.1f} MiB", flush=True)
    return result


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-sizes", nargs="+", type=positive, default=[1000, 10000, 100000])
    parser.add_argument("--scan-files", type=positive, default=10000)
    parser.add_argument("--small-files", type=positive, default=100)
    parser.add_argument("--large-mib", type=positive, default=16)
    parser.add_argument("--repeats", type=positive, default=3)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results.json"))
    parser.add_argument("--temp-parent", type=Path, help="existing local directory for generated fixtures")
    args = parser.parse_args(argv)
    # Explicit finite bounds protect against accidental enormous fixture requests.
    if (max(args.status_sizes) > 1_000_000 or args.scan_files > 1_000_000
            or args.small_files > 10_000 or args.large_mib > 1024 or args.repeats > 100):
        parser.error("limits: 1m status/scan entries, 10k small files, 1024 MiB large file, 100 repeats")
    if args.output.exists():
        parser.error("output already exists; choose another name (existing results are never overwritten)")
    if not args.output.parent.is_dir():
        parser.error("output parent directory must already exist")
    if args.temp_parent is not None and not args.temp_parent.is_dir():
        parser.error("--temp-parent must be an existing directory")
    return args


def main(argv=None):
    args = parse_args(argv)
    report = {
        "schema_version": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "machine": platform.machine(), "cpu_count": os.cpu_count(),
                        "processor": platform.processor()},
        "parameters": {"status_sizes": args.status_sizes, "scan_files": args.scan_files,
                       "small_files": args.small_files, "large_mib": args.large_mib,
                       "repeats": args.repeats},
        "source_sha256": IMPORTED_SOURCE_HASHES,
        "exclusions": [
            "No real SVN executable, subprocess launch, repository, authentication or network operation.",
            "No GUI rendering, Windows-native behavior, installer, shell integration or frame timing.",
            "Memory is separate-run Python tracemalloc, not total RSS/working set or child-process memory.",
            "Fixtures, XML and existing record inputs are prepared outside measured memory/time windows.",
            "Warmups and repeated runs warm filesystem caches; these are not cold-disk throughput tests.",
            "Shelf SVN metadata is simulated; BASE is literal fixture content without EOL/keyword translation.",
            "Shelf data I/O, compression, hashing, manifests, preflight and restore are production code.",
            "Large shelf cases are unversioned. They do not measure whole-file svn cat BASE buffering.",
            "A shelf list measurement contains one shelf, not an accumulated large shelf collection.",
            "local_files is a retained helper/comparison; current UI uses lazy directory_entries, not this full recursive scan.",
            "Refresh candidate scan uses 80% clean/20% modified existing files; its SVN response is simulated.",
        ],
    }
    with tempfile.TemporaryDirectory(prefix="svn-desk-benchmark-", dir=args.temp_parent) as temporary:
        root = Path(temporary)
        report["status"] = benchmark_status(root, args.status_sizes, args.repeats)
        report["scan"] = benchmark_scan(root / "scan", args.scan_files, args.repeats)
        report["shelves"] = [
            benchmark_shelf(root, "small-modified", args.small_files, 4096, args.repeats,
                            versioned=True, compressible=True),
            benchmark_shelf(root, "large-compressible", 1, args.large_mib * MIB, args.repeats,
                            versioned=False, compressible=True),
            benchmark_shelf(root, "large-high-entropy", 1, args.large_mib * MIB, args.repeats,
                            versioned=False, compressible=False),
        ]
    report["source_changed_during_run"] = source_hashes() != IMPORTED_SOURCE_HASHES
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    # Exclusive creation also protects against another runner winning the output race.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Saved {args.output}", flush=True)
    if report["source_changed_during_run"]:
        print("WARNING: implementation files changed during the run; rerun on stable sources.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

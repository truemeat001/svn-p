#!/usr/bin/env python3
"""Compare prior full-tree parsing with current incremental status parsing.

Synthetic XML only: no installed SVN, repository, subprocess, network or GUI.
Input generation is outside timing/memory windows. Run from the project root:
  python benchmarks/benchmark_status_stream.py --output benchmarks/new-results.json
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from benchmark_client import measure, status_fixture  # noqa: E402
from svn_client.backend import CommandResult, StatusEntry, SvnClient, _int  # noqa: E402
from svn_client.status_stream import CHUNK_BYTES, PIPE_QUEUE_CHUNKS, DIAGNOSTIC_BYTES  # noqa: E402


def fingerprints():
    names = ["svn_client/backend.py", "svn_client/status_stream.py",
             "benchmarks/benchmark_status_stream.py", "benchmarks/benchmark_client.py"]
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def previous_full_tree(xml, relative):
    """Frozen pre-streaming algorithm; same StatusEntry dataclass and semantics."""
    root = ET.fromstring(xml)
    changelists = {id(entry): group.get("name") for group in root.findall(".//changelist")
                  for entry in group.findall("entry")}
    results = []
    for entry in root.findall(".//entry"):
        status = entry.find("wc-status")
        if status is None:
            continue
        commit = status.find("commit")
        results.append(StatusEntry(
            path=relative(entry.get("path", "")), item=status.get("item", "none"),
            props=status.get("props", "none"), revision=_int(status.get("revision")),
            tree_conflicted=status.get("tree-conflicted") == "true",
            copied=status.get("copied") == "true", switched=status.get("switched") == "true",
            wc_locked=status.get("wc-locked") == "true", moved_from=status.get("moved-from"),
            moved_to=status.get("moved-to"), changelist=changelists.get(id(entry)),
            file_external=status.get("file-external") == "true",
            last_changed_revision=_int(commit.get("revision")) if commit is not None else None,
            last_changed_author=status.findtext("commit/author", ""),
        ))
    return results


def benchmark(root, count, repeats):
    xml, expected_counts = status_fixture(count)
    client = object.__new__(SvnClient)
    client.working_copy = root
    client.command = lambda args: CommandResult(tuple(args), 0, xml, "")
    previous = lambda: previous_full_tree(xml, client._relative)
    current = lambda: client.status(verbose=True)
    before_rows, after_rows = previous(), current()
    if before_rows != after_rows or len(after_rows) != count:
        raise AssertionError("Entry order or metadata changed between parsers")
    if dict(Counter(row.item for row in after_rows)) != expected_counts:
        raise AssertionError("Status fixture counts changed")
    del before_rows, after_rows
    before = measure(previous, repeats)
    after = measure(current, repeats)
    row = {"entries": count, "xml_utf8_bytes": len(xml.encode("utf-8")),
           "all_entries_and_order_equal": True, "status_counts": expected_counts,
           "previous_full_tree": before, "incremental_status": after,
           "peak_allocation_reduction_percent": round(100 * (1 - after["peak_traced_mib"] / before["peak_traced_mib"]), 2),
           "median_time_change_percent": round(100 * (after["median_ms"] / before["median_ms"] - 1), 2)}
    print(f"{count:,} entries: {before['median_ms']:.1f} -> {after['median_ms']:.1f} ms, "
          f"{before['peak_traced_mib']:.1f} -> {after['peak_traced_mib']:.1f} MiB traced peak", flush=True)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000, 100000])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.sizes or min(args.sizes) < 1 or max(args.sizes) > 1_000_000 or not 1 <= args.repeats <= 100:
        parser.error("Use 1..1,000,000 entries and 1..100 repeats")
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("Output must be a new file in an existing directory")
    before_hashes = fingerprints()
    report = {"schema_version": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "platform": platform.platform()},
              "source_sha256": before_hashes,
              "parameters": {"sizes": args.sizes, "repeats": args.repeats},
              "transport_bounds": {"stdout_chunk_bytes": CHUNK_BYTES,
                                   "stdout_queue_chunks": PIPE_QUEUE_CHUNKS,
                                   "stderr_retained_bytes": DIAGNOSTIC_BYTES},
              "exclusions": ["Synthetic XML, no real SVN or subprocess timings, no network or GUI.",
                             "Input XML prepared before measurements; both retain the complete result list.",
                             "Current parser is actual status() using the compatible injected command transport.",
                             "Prior parser is the frozen preceding full-tree algorithm included in this script.",
                             "Python tracemalloc only; no total RSS, native allocation, OS pipes, or child memory.",
                             "Warmup once, independent untraced timings, then one separately traced execution.",
                             "Queue/diagnostic bounds are implementation limits, not measurements of this in-process run."]}
    with tempfile.TemporaryDirectory(prefix="svn-status-benchmark-") as temporary:
        report["status"] = [benchmark(Path(temporary), count, args.repeats) for count in args.sizes]
    report["source_changed_during_run"] = fingerprints() != before_hashes
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Saved {args.output}", flush=True)
    return 2 if report["source_changed_during_run"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fast harness checks, separate from the client's own regression tests."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import benchmark_client as bench


class BenchmarkHarnessTests(unittest.TestCase):
    def test_mixed_status_fixture_uses_production_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            xml, counts = bench.status_fixture(100)
            entries = bench.fixture_client(Path(temporary), xml).status(verbose=True)
        self.assertEqual(len(entries), 100)
        self.assertEqual(counts["normal"], 80)
        self.assertEqual(counts["modified"], 12)
        self.assertTrue(any(entry.changelist for entry in entries))
        self.assertTrue(any(entry.tree_conflicted for entry in entries))
        self.assertTrue(all("@" in entry.path for entry in entries))

    def test_measure_excludes_setup_and_keeps_samples(self):
        events = []
        def work():
            events.append("work")
            return bytearray(1000)
        result = bench.measure(work, 2, prepare=lambda: events.append("prepare"),
                               cleanup=lambda value: events.append("cleanup"))
        self.assertEqual(events, ["prepare", "work", "cleanup"] * 4)
        self.assertEqual(len(result["samples_ms"]), 2)
        self.assertGreaterEqual(result["peak_traced_mib"], 0)
        self.assertEqual(result["warmup_runs"], 1)

    def test_scan_excludes_markers_and_nested_working_copies(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            result = bench.benchmark_scan(Path(temporary) / "scan", 7, 1)
        self.assertTrue(result["excluded_sentinels_verified"])
        self.assertEqual(result["capped_returned"], 7)
        self.assertFalse(result["capped_truncated"])

    def test_modified_shelf_roundtrip_and_request_count_without_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            with patch("subprocess.run", side_effect=AssertionError("must not execute SVN")):
                result = bench.benchmark_shelf(Path(temporary), "modified", 3, 4096, 1,
                                               versioned=True, compressible=True)
        self.assertTrue(result["restore_hashes_verified"])
        self.assertEqual(sum(result["simulated_svn_calls"]["create"].values()), 6 * 3 + 3)
        self.assertEqual(result["blob_encodings"], {"zlib": 3})

    def test_large_unversioned_compression_and_raw_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            for compressible, encoding in ((True, "zlib"), (False, "raw")):
                result = bench.benchmark_shelf(Path(temporary), encoding, 1, bench.MIB, 1,
                                               versioned=False, compressible=compressible)
                self.assertTrue(result["restore_hashes_verified"])
                self.assertEqual(result["blob_encodings"], {encoding: 1})
                if compressible:
                    self.assertLess(result["stored_blob_bytes"], bench.MIB // 10)
                else:
                    self.assertEqual(result["stored_blob_bytes"], bench.MIB)

    def test_invalid_size_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for arguments in (["--repeats", "0"], ["--status-sizes", "1000001"],
                              ["--large-mib", "1025"]):
                with self.assertRaises(SystemExit):
                    bench.parse_args(arguments)

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing.json"
            output.write_text("keep me", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bench.parse_args(["--output", str(output)])
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_unknown_fake_command_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wc = root / "wc"
            wc.mkdir()
            manager = bench.SyntheticShelfManager(wc, root / "shelves", root / "base")
            with self.assertRaises(AssertionError):
                manager._svn("commit", "--message", "must not run")


if __name__ == "__main__":
    unittest.main()

"""Regression checks for the synthetic status comparison harness."""
import contextlib
import io
import tempfile
from pathlib import Path
import unittest

import benchmark_status_stream as bench


class StatusBenchmarkTests(unittest.TestCase):
    def test_fixture_equality_and_measurement_fields(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            result = bench.benchmark(Path(temporary), 100, 1)
        self.assertTrue(result["all_entries_and_order_equal"])
        self.assertEqual(result["entries"], 100)
        for key in ("previous_full_tree", "incremental_status"):
            self.assertEqual(result[key]["timed_runs"], 1)
            self.assertIn("peak_traced_mib", result[key])

    def test_full_tree_reference_preserves_required_metadata(self):
        xml = '<status><changelist name="甲"><entry path="f"><wc-status item="normal" file-external="true" moved-to="b"><commit revision="7"><author>乙</author></commit></wc-status></entry></changelist></status>'
        [row] = bench.previous_full_tree(xml, str)
        self.assertEqual(row.changelist, "甲")
        self.assertTrue(row.file_external)
        self.assertEqual(row.moved_to, "b")
        self.assertEqual(row.last_changed_revision, 7)
        self.assertEqual(row.last_changed_author, "乙")

    def test_invalid_parameters_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            output = Path(temporary) / "result.json"
            for arguments in (["--sizes", "0"], ["--sizes", "1000001"], ["--repeats", "0"]):
                with self.assertRaises(SystemExit):
                    bench.main([*arguments, "--output", str(output)])
            self.assertFalse(output.exists())

    def test_existing_output_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stderr(io.StringIO()):
            output = Path(temporary) / "result.json"
            output.write_text("keep", encoding="utf-8")
            with self.assertRaises(SystemExit):
                bench.main(["--output", str(output)])
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()

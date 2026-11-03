"""Diff display preserves UTF-8 segments without guessing mixed native encodings."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from svn_client.backend import SvnClient
from svn_client.history import (DIFF_TEXT_POLICY, BoundedText, ComparisonResult,
    HistoryError, HistoryManager, decode_diff_output)
from svn_client.history_ui import comparison_preview


class DiffDecodingTests(unittest.TestCase):
    def test_utf8_is_unchanged_across_platform_locales(self):
        raw = 'Index: 中文.txt\r\n@@ -1 +1 @@\r\n-旧值\r\n+新内容\t✓😀\r\n'.encode()
        for locale in ['cp936', 'cp1252', 'cp932', 'UTF-8']:
            with self.subTest(locale=locale), patch('locale.getencoding', return_value=locale):
                text, unknown, controls = decode_diff_output(raw)
            self.assertEqual(text.encode(), raw); self.assertEqual((unknown, controls), (0, 0))

    def test_native_cp936_headers_do_not_corrupt_utf8_hunks(self):
        header = 'Index: 中文.txt\n--- 中文.txt\n+++ 中文.txt\n'.encode('cp936')
        body = '@@ -1 +1 @@\n-旧正文\n+新正文✓\n'.encode('utf-8')
        text, unknown, controls = decode_diff_output(header + body)
        self.assertIn(body.decode(), text)
        self.assertIn(r'\xd6\xd0\xce\xc4.txt', text)
        self.assertEqual(unknown, 12); self.assertEqual(controls, 0)
        self.assertNotIn('�', text)

    def test_cp932_cp1252_and_intraline_invalid_bytes_are_visible_not_guessed(self):
        for raw in [b'+caf\xe9\n', b'Index: \x93\xfa\x96{.txt\n', b'+valid \xff' + '中文'.encode() + b'\x80 end\n']:
            with self.subTest(raw=raw):
                text, unknown, controls = decode_diff_output(raw)
                self.assertGreater(unknown, 0); self.assertIn('\\x', text); self.assertNotIn('�', text)
        self.assertIn('中文', decode_diff_output(b'\xff' + '中文'.encode() + b'\xff')[0])

    def test_partial_utf8_and_existing_escape_text_have_explicit_warning(self):
        text, unknown, _ = decode_diff_output(b'literal \\xff and actual \xff\xe4\xb8')
        self.assertEqual(unknown, 3)
        self.assertEqual(text, r'literal \xff and actual \xff\xe4\xb8')
        # Display is intentionally not claimed reversible or an exact patch.

    def test_control_characters_are_visible_but_crlf_tabs_newlines_preserved(self):
        text, unknown, controls = decode_diff_output(b'line\r\n\tvalue\n\x1b[31m\x7f\r' + '\u0085'.encode())
        self.assertEqual(text, 'line\r\n\tvalue\n\\x1b[31m\\x7f\\x0d\\u0085')
        self.assertEqual((unknown, controls), (0, 4))

    def test_nul_or_utf16_never_returns_partial_text(self):
        for raw in [b'valid prefix\n\x00binary', '正文'.encode('utf-16'), b'Index: a\n+\xff\x00']:
            with self.subTest(raw=raw), self.assertRaisesRegex(HistoryError, 'NUL'):
                decode_diff_output(raw)

    def test_binary_notice_is_text_not_binary_payload(self):
        raw = b'Cannot display: file marked as a binary type.\nsvn:mime-type = application/octet-stream\n'
        self.assertEqual(decode_diff_output(raw), (raw.decode(), 0, 0))

    def test_chunk_boundaries_preserve_crlf_utf8_and_count_every_escape(self):
        raw = b'a' * (64 * 1024 - 1) + b'\r\n' + ('中文' * 40000).encode() + b'\xff' * (64 * 1024 + 1)
        text, unknown, controls = decode_diff_output(raw)
        self.assertTrue(text.startswith('a' * (64 * 1024 - 1) + '\r\n中文'))
        self.assertIn('中文' * 40000, text)
        self.assertTrue(text.endswith(r'\xff' * (64 * 1024 + 1)))
        self.assertEqual((unknown, controls), (64 * 1024 + 1, 0))
        self.assertEqual(decode_diff_output(b''), ('', 0, 0))

    def test_locale_ambiguity_does_not_create_a_false_locale_claim(self):
        # Some legacy bytes also form valid UTF-8. No decoder can infer intent.
        # The policy reports only UTF-8 interpretation, never original encoding.
        self.assertEqual(decode_diff_output(b'\xc2\xa9'), ('©', 0, 0))
        self.assertEqual(DIFF_TEXT_POLICY, 1)


class ComparisonEncodingTests(unittest.TestCase):
    def manager(self, raw):
        # Only synthetic read transport; native fallback must not be consulted.
        client = SimpleNamespace(_lock=__import__('threading').RLock())
        manager = HistoryManager(client)
        manager._target = lambda path: (str(path), str(path))
        manager._check_target = lambda path: None
        manager._run = lambda args, limit: raw
        return manager

    def test_compare_preserves_mixed_text_metadata_for_local_and_numeric_revisions(self):
        raw = 'Index: 中文.txt\n'.encode('cp936') + '+新正文\n'.encode()
        for left, right in [('BASE', 'WORKING'), ('1', '2'), ('HEAD', '1')]:
            with self.subTest(left=left,right=right), patch.object(SvnClient, '_decode', side_effect=AssertionError('whole-output native fallback forbidden')):
                result = self.manager(raw).compare('中文.txt', from_revision=left, to_revision=right)
            self.assertIn('+新正文', result.diff.text)
            self.assertEqual(result.diff.text_encoding, 'utf-8-visible-escapes')
            self.assertEqual(result.diff.undecodable_bytes, 4); self.assertEqual(result.diff.escaped_controls, 0)
            self.assertEqual(result.diff.returned_utf8_bytes, len(result.diff.text.encode()))
            self.assertFalse(result.binary_payloads_included)

    def test_warning_survives_truncation_before_unknown_bytes(self):
        result = self.manager(b'0123456789' + b'\xff').compare('a', max_text_bytes=5)
        self.assertEqual(result.diff.text, '01234'); self.assertTrue(result.diff.truncated)
        self.assertEqual(result.diff.undecodable_bytes, 1)
        self.assertEqual(result.diff.text_encoding, 'utf-8-visible-escapes')

    def test_credentials_stay_redacted_after_byte_escaping(self):
        result = self.manager(b'https://user:\xffSUPERSECRET@host\n').compare('a')
        self.assertNotIn('SUPERSECRET', result.diff.text); self.assertIn('***@host', result.diff.text)
        self.assertGreater(result.diff.undecodable_bytes, 0)

    def test_base_to_base_rejected_before_any_target_or_transport(self):
        manager = self.manager(b'not used')
        manager._target = lambda _: self.fail('No target resolution for unsupported pair')
        with self.assertRaisesRegex(ValueError, 'BASE to BASE'):
            manager.compare('a', from_revision='BASE', to_revision='BASE')

    def test_native_preview_always_exposes_encoding_warning(self):
        diff = BoundedText(r'\xff', False, 4, 4, 'utf-8-visible-escapes', 1, 0)
        result = ComparisonResult('a', 'BASE', 'WORKING', diff)
        shown = comparison_preview(result, 2)
        self.assertIn('编码警告', shown); self.assertIn('不能作为精确补丁', shown)
        self.assertIn('已截断', shown)


if __name__ == '__main__': unittest.main()

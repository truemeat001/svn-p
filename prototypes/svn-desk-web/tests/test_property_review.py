"""Independent synthetic property parser/display contract review; no SVN calls."""
import base64
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from property_reads import _properties, _value


def xml_property(name, value, encoding=None):
    root = ET.Element('properties')
    target = ET.SubElement(root, 'target', path='a.txt')
    prop = ET.SubElement(target, 'property', name=name)
    if encoding is not None:
        prop.set('encoding', encoding)
    prop.text = value
    return ET.tostring(root)


class IndependentPropertyReview(unittest.TestCase):
    def test_empty_and_absent_are_distinct(self):
        self.assertEqual(_properties(b'<properties/>', Path('a.txt')), {})
        self.assertEqual(_properties(xml_property('empty', ''), Path('a.txt')), {'empty': b''})
        self.assertEqual(_value(b'', lambda s: s)['kind'], 'text')

    def test_base64_raw_line_endings_and_binary_survive(self):
        for raw in [b'one\r\ntwo\rthree\n', bytes(range(256)), '😀\u202e\u200b'.encode()]:
            encoded = base64.b64encode(raw).decode()
            wrapped = '\n\t'.join(encoded[i:i+4] for i in range(0, len(encoded), 4))
            self.assertEqual(_properties(xml_property('x', wrapped, 'base64'), Path('a.txt')), {'x': raw})

    def test_extended_control_categories_are_escaped_or_refused(self):
        for char in ['\u00ad', '\u200b', '\u2060', '\ufeff', '\U000e0001']:
            with self.subTest(char=repr(char)):
                with self.assertRaises(ValueError):
                    _properties(xml_property('x' + char, 'v'), Path('a.txt'))
                value = _value(('a' + char + 'b').encode(), lambda s: s)
                self.assertNotIn(char, value['text'])
                self.assertEqual(value['escapedControls'], 1)

    def test_astral_names_and_text_preserve_utf8(self):
        name = '😀' * 128
        self.assertEqual(_properties(xml_property(name, '😀'), Path('a.txt')), {name: '😀'.encode()})
        value = _value(('😀' * 2000).encode(), lambda s: s)
        self.assertEqual(value['text'], '😀' * 1024)
        self.assertTrue(value['textTruncated'])

    def test_structural_noise_is_not_empty_success(self):
        for xml in [
            '<properties><target path="a.txt">noise</target></properties>',
            '<properties><target path="a.txt"/>noise</properties>',
            '<properties><target path="a.txt"><property name="x"/>noise</target></properties>',
            '<properties><target path="a.txt"><property name="x" bogus="1"/></target></properties>',
            '<properties><target path="a.txt"><property name="x"><!--noise--></property></target></properties>',
            '<properties><target path="a.txt"><property name="x"><?noise x?></property></target></properties>',
            '<properties xmlns="unexpected"/>',
        ]:
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                _properties(xml.encode(), Path('a.txt'))


if __name__ == '__main__':
    unittest.main()

"""Independent XML fingerprint review using synthetic files and mocked SVN."""
import copy
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import test_commits


@unittest.skipUnless((test_commits.BACKEND / 'svn_client/history.py').is_file(), 'Paired backend unavailable.')
class CommitXmlFingerprintReview(unittest.TestCase):
    def setUp(self):
        self.fixture = test_commits.CommitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def fingerprint(self, entry):
        f = self.fixture
        f.manager._info = Mock(return_value=(entry, None))
        return f.service._metadata({test_commits.native('src/a.txt'): {}})

    def test_only_attribute_order_is_discarded_recursively(self):
        original = ET.fromstring('<entry kind="file" revision="1" path="src/a.txt">head'
                                 '<repository x="x" y="y"><uuid>fixture</uuid></repository>tail'
                                 '<wc-info><schedule>normal</schedule></wc-info></entry>')
        reordered = copy.deepcopy(original)
        for node in reordered.iter():
            pairs = list(node.attrib.items())[::-1]
            node.attrib.clear()
            node.attrib.update(pairs)
        self.assertNotEqual(ET.tostring(original), ET.tostring(reordered))
        self.assertEqual(self.fingerprint(original), self.fingerprint(reordered))

    def test_all_non_order_xml_information_remains_bound(self):
        original = ET.fromstring('<entry kind="file" revision="1">head'
                                 '<repository x="x" y="y"><uuid>fixture</uuid></repository>tail'
                                 '<wc-info><schedule>normal</schedule></wc-info></entry>')
        mutations = {
            'entry attribute value': lambda n: n.set('revision', '2'),
            'nested attribute value': lambda n: n[0].set('x', 'changed'),
            'new attribute': lambda n: n[0].set('extra', 'value'),
            'missing attribute': lambda n: n[0].attrib.pop('x'),
            'tag': lambda n: setattr(n[0], 'tag', 'renamed'),
            'text': lambda n: setattr(n[0][0], 'text', 'another-repository'),
            'tail': lambda n: setattr(n[0], 'tail', 'changed'),
            'child order': lambda n: n.__setitem__(slice(None), list(n)[::-1]),
            'added element': lambda n: ET.SubElement(n, 'copy-from-rev'),
            'removed element': lambda n: n.remove(n[1]),
        }
        baseline = self.fingerprint(original)
        for label, mutate in mutations.items():
            with self.subTest(change=label):
                changed = copy.deepcopy(original)
                mutate(changed)
                self.assertNotEqual(baseline, self.fingerprint(changed))

    def test_alternating_attribute_order_through_review_and_apply(self):
        f = self.fixture
        original = f.info_xml
        calls = 0
        def alternating(target):
            nonlocal calls
            calls += 1
            root = ET.fromstring(original(target))
            entry = root.find('entry')
            entry.set('path', target)
            repo = entry.find('repository')
            repo.set('one', '1')
            repo.set('two', '2')
            if calls % 2:
                for node in root.iter():
                    pairs = list(node.attrib.items())[::-1]
                    node.attrib.clear()
                    node.attrib.update(pairs)
            return ET.tostring(root)
        f.manager._run = Mock(side_effect=lambda args, limit: alternating(args[-1]))
        f.apply(f.preflight())
        f.client.command.assert_called_once()
        self.assertGreater(calls, 4)


if __name__ == '__main__':
    unittest.main()

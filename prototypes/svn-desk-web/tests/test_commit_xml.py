"""Stable XML metadata identities; synthetic temp fixtures, no SVN executable."""
import copy
import itertools
import hashlib
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import test_commits
from commit_ops import _xml_identity
from file_reads import StaleRead


class XMLIdentityTests(unittest.TestCase):
    def test_every_attribute_permutation_is_equivalent(self):
        node = ET.fromstring('<entry kind="file" revision="1" path="a" extra="x"><repository a="1" b="2"><uuid>fixture</uuid></repository></entry>')
        expected = _xml_identity(node)
        for attributes in itertools.permutations(node.attrib.items()):
            reordered = copy.deepcopy(node)
            reordered.attrib = dict(attributes)
            reordered.find('repository').attrib = {'b':'2','a':'1'}
            self.assertEqual(_xml_identity(reordered),expected)
        self.assertEqual(list(node.attrib),['kind','revision','path','extra'])

    def test_attributes_text_tail_tags_and_child_order_are_preserved(self):
        source = ET.fromstring('<entry revision="1" extra="original">text<first a="1">one</first>tail<second>two</second></entry>')
        def reverse_children(node):
            node[:] = list(reversed(node))
        mutations = [
            lambda n:n.set('revision','2'), lambda n:n.set('extra','changed'),
            lambda n:n.set('added','value'), lambda n:n.attrib.pop('extra'),
            lambda n:setattr(n,'text','changed'), lambda n:setattr(n[0],'tail','changed'),
            lambda n:setattr(n[0],'tag','renamed'), lambda n:n[0].set('a','2'),
            reverse_children,
            lambda n:n.remove(n[1]),
        ]
        expected = _xml_identity(source)
        for mutation in mutations:
            node = copy.deepcopy(source); mutation(node)
            self.assertNotEqual(_xml_identity(node),expected)


@unittest.skipUnless((test_commits.BACKEND/'svn_client/history.py').is_file(),'Paired backend unavailable.')
class CommitXMLTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_commits.CommitTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.original = self.fixture.info_xml

    def varying_info(self,target):
        tree = ET.fromstring(self.original(target)); entry = tree.find('entry')
        entry.set('path',target); entry.find('repository').attrib = {'first':'1','second':'2'}
        attributes = list(entry.attrib.items()); turn = self.turn % len(attributes); self.turn += 1
        entry.attrib = dict(attributes[turn:]+attributes[:turn])
        if self.turn % 2: entry.find('repository').attrib = {'second':'2','first':'1'}
        return ET.tostring(tree)

    def install_varying_info(self):
        self.turn = 0
        self.fixture.manager._run = Mock(side_effect=lambda args,limit:self.varying_info(args[-1]))

    def test_fixture_reproduces_unstable_raw_xml_serialization(self):
        self.install_varying_info()
        values = [ET.tostring(ET.fromstring(self.varying_info('src/a.txt')).find('entry')) for _ in range(8)]
        # Nested attribute order also varies, so raw serialization has multiple
        # variants even though all eight parsed metadata values are equivalent.
        self.assertGreater(len({hashlib.sha256(value).digest() for value in values}),1)
        self.assertEqual(len({repr(_xml_identity(ET.fromstring(value))) for value in values}),1)

    def test_eight_equivalent_reads_share_one_digest(self):
        self.install_varying_info(); f = self.fixture
        expected = {test_commits.native('src/a.txt'): f.snapshot[1][test_commits.native('src/a.txt')]}
        results = [tuple(f.service._metadata(expected).items()) for _ in range(8)]
        self.assertEqual(len(set(results)),1)
        f.client.command.assert_not_called()

    def test_varying_order_preflight_and_apply_selected(self):
        self.install_varying_info(); f = self.fixture
        result = f.apply(f.preflight())
        self.assertTrue(result['commandCompleted']); f.client.command.assert_called_once()

    def test_varying_order_preflight_and_apply_complete_changelist(self):
        self.install_varying_info(); f = self.fixture
        result = f.apply(f.preflight(operation='changelist',name='Feature',
            ids=[test_commits.native('src/a.txt'),test_commits.native('src/b @.txt')]))
        self.assertEqual(len(result['files']),2); f.client.command.assert_called_once()

    def test_real_info_values_still_invalidate_plan(self):
        f = self.fixture
        mutations = [
            lambda n:n.set('revision','2'), lambda n:n.set('extra','changed'),
            lambda n:n.attrib.pop('extra'), lambda n:n.set('new-attribute','new'),
            lambda n:setattr(n.find('repository/uuid'),'text','different-repository'),
            lambda n:n.find('repository').set('flag','changed'),
            lambda n:setattr(n.find('wc-info/schedule'),'text','replace'),
            lambda n:setattr(n.find('wc-info/copy-from-rev'),'text','2'),
            lambda n:n.find('wc-info').remove(n.find('wc-info/copy-from-rev')),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                active = [None]
                def response(target):
                    tree=ET.fromstring(self.original(target)); entry=tree.find('entry')
                    entry.set('extra','original'); entry.find('repository').set('flag','original')
                    ET.SubElement(entry.find('wc-info'),'schedule').text='normal'
                    ET.SubElement(entry.find('wc-info'),'copy-from-rev').text='1'
                    if active[0]: active[0](entry)
                    return ET.tostring(tree)
                f.manager._run = Mock(side_effect=lambda args,limit:response(args[-1]))
                plan=f.preflight(); active[0]=mutation
                with self.assertRaises(StaleRead): f.apply(plan)
                f.client.command.assert_not_called()

    def test_same_status_property_change_rejected_with_varying_info_order(self):
        self.install_varying_info(); f=self.fixture
        f.props={'custom':'old'}; plan=f.preflight(); f.props={'custom':'new'}
        with self.assertRaises(StaleRead): f.apply(plan)
        f.client.command.assert_not_called()


if __name__=='__main__': unittest.main()

"""模型输入去重回归：合成来源，使用实际索引构造函数；不调用外部服务。"""
from copy import deepcopy
import json
import unittest

from support_context import compact_support_tool_view
from support_runtime import citation_index


class SupportContextTests(unittest.TestCase):
    def fact(self):
        # 字段结构对应真实售后工具；文本、文件位置及编号均为公开合成样例。
        common = {'document_id': 'TEST-POLICY', 'document_checksum': 'document-checksum',
                  'version': '2026-11-v1', 'store_id': 'S_TEST', 'category': 'ordinary_shoes',
                  'source_kind': 'synthetic_demo', 'source_path': 'knowledge/test-policy.md'}
        returns = {**common, 'chunk_id': 'TEST:return', 'citation_id': 'TEST:return',
                   'section': 'return_eligibility', 'line_start': 4, 'line_end': 7,
                   'text': '收货起336小时内，须未穿着且吊牌完好；定制商品不适用。',
                   'checksum': 'return-checksum'}
        shipping = {**common, 'chunk_id': 'TEST:shipping', 'citation_id': 'TEST:shipping',
                    'section': 'shipping_cost', 'line_start': 9, 'line_end': 11,
                    'text': '尺码原因由买家承担；商家责任需要核实记录。', 'checksum': 'shipping-checksum'}
        fact = {'status': 'ok', 'order_id': 'O_TEST', 'requested_topics': ['eligibility', 'shipping'],
                'trusted_facts': {'delivered_at': '2026-11-16T02:00:00+00:00'},
                'user_claims': {'values': {'is_unworn': True}, 'verified': False},
                'missing': ['tags_intact'], 'policy_version': '2026-11-v1',
                'citations': {key: deepcopy(returns) for key in
                              ('return_window_hours', 'requires_unworn', 'requires_tags', 'custom_excluded')},
                'retrieval': {'results': [{'chunk_id': 'TEST:return', 'text': returns['text']}],
                              'keyword_results': [{'chunk_id': 'TEST:return', 'score': 2.0}]},
                'required_citation_ids': ['TEST:return', 'TEST:shipping'],
                'answer_contract': {'schema_version': 2, 'claims': {
                    'custom_exception': {'text': '定制商品不适用。', 'citation_ids': ['TEST:return'], 'required': True}}}}
        fact['citations']['shipping_by_reason'] = deepcopy(shipping)
        fact['citation_index'] = citation_index(fact)
        return fact

    def test_actual_index_shape_compacts_only_repeated_sources(self):
        fact = self.fact()
        view = compact_support_tool_view(fact)
        expected = {rule: {'citation_id': source['chunk_id']} for rule, source in fact['citations'].items()}
        self.assertEqual(view['citations'], expected)
        self.assertEqual({key: value for key, value in view.items() if key != 'citations'},
                         {key: value for key, value in fact.items() if key != 'citations'})
        self.assertLess(len(json.dumps(view, ensure_ascii=False)), len(json.dumps(fact, ensure_ascii=False)))

    def test_no_aliases_or_mutation_of_original_evidence(self):
        fact = self.fact()
        before = deepcopy(fact)
        view = compact_support_tool_view(fact)
        self.assertEqual(fact, before)
        view['citation_index']['TEST:return']['text'] = 'changed'
        view['trusted_facts']['delivered_at'] = None
        view['answer_contract']['claims']['custom_exception']['citation_ids'].clear()
        self.assertEqual(fact, before)

    def test_each_compact_reference_resolves_to_complete_original_source(self):
        fact = self.fact()
        view = compact_support_tool_view(fact)
        for rule, source in fact['citations'].items():
            canonical = view['citation_index'][view['citations'][rule]['citation_id']]
            self.assertEqual(source, {key: value for key, value in canonical.items() if key != 'supports'})
            self.assertIn(rule, canonical['supports'])
        for ref in view['required_citation_ids']:
            self.assertIn(ref, view['citation_index'])
        for claim in view['answer_contract']['claims'].values():
            for ref in claim['citation_ids']:
                self.assertIn(ref, view['citation_index'])
        self.assertIn('定制商品不适用', view['citation_index']['TEST:return']['text'])

    def test_unknown_or_missing_shape_returns_unchanged_deep_copy(self):
        variants = [None, [], {}, {'citations': []}, {'citations': {}, 'citation_index': {}}]
        for key, value in (('citations', []), ('citation_index', None)):
            fact = self.fact()
            fact[key] = value
            variants.append(fact)
        fact = self.fact()
        fact['citations']['requires_tags'] = {'citation_id': 'TEST:return'}
        variants.append(fact)
        fact = self.fact()
        del fact['citation_index']['TEST:shipping']
        variants.append(fact)
        for value in variants:
            with self.subTest(value_type=type(value).__name__):
                before = deepcopy(value)
                view = compact_support_tool_view(value)
                self.assertEqual(view, before)
                self.assertEqual(value, before)
                if isinstance(value, (dict, list)):
                    self.assertIsNot(view, value)

    def test_conflicting_source_never_gets_hidden_by_index(self):
        for key, changed in (('text', '另一条政策'), ('version', 'old-version'),
                             ('checksum', 'other-checksum'), ('line_start', True),
                             ('extra_metadata', {'source': 'only-one-side'})):
            fact = self.fact()
            if key == 'line_start':
                # Python里True == 1，但原始JSON来源元数据并不相同。
                fact['citation_index']['TEST:return'][key] = 1
                for source in fact['citations'].values():
                    if source['chunk_id'] == 'TEST:return':
                        source[key] = 1
            fact['citations']['requires_tags'][key] = changed
            with self.subTest(field=key):
                self.assertEqual(compact_support_tool_view(fact), fact)

    def test_wrong_derived_supports_or_reference_returns_all_original_sources(self):
        for supports in (None, [], ['requires_unworn'], ['requires_tags', 'requires_tags'], [1]):
            fact = self.fact()
            fact['citation_index']['TEST:return']['supports'] = supports
            with self.subTest(supports=supports):
                self.assertEqual(compact_support_tool_view(fact), fact)
        fact = self.fact()
        fact['citations']['requires_tags']['citation_id'] = 'TEST:shipping'
        self.assertEqual(compact_support_tool_view(fact), fact)
        fact = self.fact()
        fact['citation_index']['UNEXPECTED'] = deepcopy(fact['citation_index']['TEST:return'])
        self.assertEqual(compact_support_tool_view(fact), fact)

    def test_fallback_and_already_compact_views_remain_independent_copies(self):
        fact = self.fact()
        fact['citation_index']['TEST:return']['text'] = 'conflict'
        view = compact_support_tool_view(fact)
        view['citations']['requires_tags']['text'] = 'changed'
        self.assertNotEqual(view['citations'], fact['citations'])
        compact = compact_support_tool_view(self.fact())
        second = compact_support_tool_view(compact)
        self.assertEqual(second, compact)
        second['citation_index']['TEST:return']['supports'].clear()
        self.assertNotEqual(second, compact)


if __name__ == '__main__':
    unittest.main()

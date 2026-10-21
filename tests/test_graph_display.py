"""Exercise graph rendering with the real frontend module and a captured network."""
import importlib.util
import json
from html.parser import HTMLParser
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(importlib.util.find_spec('streamlit'), 'Install frontend requirements')
class GraphDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('litkg_frontend_graph_test',
            Path(__file__).resolve().parents[1] / 'frontend/app.py')
        cls.ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ui)

    def test_parallel_labels_expose_direction_polarity_and_distinct_contexts(self):
        edges = [{'id': i, 'source': 1, 'target': 2, 'label': 'DIFFERS_BETWEEN',
                  'qualifiers': {'metric': 'abundance', 'direction': direction}}
                 for i, direction in [(9, 'increased'), (7, 'decreased')]]
        edges[0]['speculative'] = True
        edges[1]['negated'] = True
        labels = self.ui.graph_edge_labels(edges)
        self.assertNotEqual(labels[9], labels[7])
        self.assertIn('increased', labels[9])
        self.assertIn('decreased', labels[7])
        self.assertIn('推测', labels[9])
        self.assertIn('否定', labels[7])
        self.assertEqual(labels, self.ui.graph_edge_labels(list(reversed(edges))))

    def test_render_keeps_all_edges_and_escapes_full_qualifiers(self):
        graph = {'nodes': [{'id': 1, 'label': 'A', 'type': 'Protein'},
                           {'id': 2, 'label': 'B', 'type': 'Protein'}],
                 'edges': [{'id': i, 'source': 1, 'target': 2, 'label': 'AFFECTS',
                            'qualifiers': {'condition': value}}
                           for i, value in [(1, '<script>bad</script>'), (2, 'other')]]}
        network = Mock()
        with patch.object(self.ui, 'Network', return_value=network), patch.object(self.ui, 'st'), patch.object(self.ui, 'components'):
            self.ui.render_graph(graph)
        self.assertEqual(network.add_edge.call_count, 2)
        first, second = [call.kwargs for call in network.add_edge.call_args_list]
        self.assertNotEqual(first['label'], second['label'])
        self.assertEqual([first['id'], second['id']], [1, 2])
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', first['title'])
        self.assertNotIn('<script>', first['title'])
        self.assertIn('other', second['title'])

    def test_single_edge_keeps_compact_predicate_and_evidence_level(self):
        self.assertEqual(self.ui.graph_edge_labels([{'id': 1, 'source': 1, 'target': 2,
            'label': 'ASSOCIATED_WITH', 'qualifiers': {'evidence_type': 'experimental'}}]),
            {1: 'ASSOCIATED_WITH [experimental]'})

    def test_evidence_design_styles_do_not_imply_experimental_validation(self):
        patterns = {}
        for kind in ['experimental','observational','computational_prediction']:
            edge={'id':1,'source':1,'target':2,'predicate':'PRODUCES','qualifiers':{'evidence_type':kind}}
            patterns[kind]=self.ui.graph_edge_dashes(edge)
            self.assertIn(kind,self.ui.graph_edge_labels([edge])[1])
        self.assertFalse(patterns['experimental'])
        self.assertNotEqual(patterns['observational'],patterns['computational_prediction'])

    def test_single_edge_shows_aspect_direction_comparison_and_review_without_full_json(self):
        edge = {'id':1, 'source':1, 'target':2, 'label':'AFFECTS', 'review_status':'pending',
            'result_status':'not_significant', 'qualifiers': {'metric':'abundance', 'reference_group':'control',
                'processes':[{'role':'object','kind':'abundance','direction':'unspecified'}],
                'dose':{'value':5, 'unit':'μg'}}}
        label = self.ui.graph_edge_labels([edge])[1]
        self.assertIn('abundance',label)
        self.assertIn('unspecified',label)
        self.assertIn('control',label)
        self.assertIn('未显著差异',label)
        self.assertIn('待审核',label)
        self.assertNotIn('dose',label)
        edge['result_status']='no_effect_reported'
        self.assertIn('未见变化',self.ui.graph_edge_labels([edge])[1])

    def test_all_microbiome_types_have_unique_colors_and_legend_uses_current_subgraph(self):
        from app.entity_types import TYPE_DEFINITIONS
        self.assertEqual(set(self.ui.ENTITY_COLORS), set(TYPE_DEFINITIONS))
        self.assertEqual(len(set(self.ui.ENTITY_COLORS.values())), len(TYPE_DEFINITIONS))
        graph = {'nodes': [{'id': 1, 'label': 'A', 'type': 'MicrobialStrain'},
                           {'id': 2, 'label': 'B', 'type': '<unknown>'}],
                 'edges': [{'id': 1, 'source': 1, 'target': 2, 'label': 'AFFECTS'}]}
        network = Mock()
        with patch.object(self.ui, 'Network', return_value=network), patch.object(self.ui, 'st') as st, patch.object(self.ui, 'components'):
            self.ui.render_graph(graph)
        legend = st.markdown.call_args.args[0]
        self.assertIn('MicrobialStrain', legend)
        self.assertIn('&lt;unknown&gt;', legend)
        self.assertNotIn('MicrobialCommunity', legend)
        self.assertEqual(network.add_node.call_args_list[0].kwargs['color']['border'], self.ui.ENTITY_COLORS['MicrobialStrain'])
        self.assertEqual(network.add_node.call_args_list[1].kwargs['color']['border'], self.ui.ENTITY_COLORS['Other'])

    def test_labels_wrap_without_losing_identifiers_or_chinese(self):
        for label in ['MicrobialStrain-' + 'Q1234567890' * 8, '蛋白质修饰位点与实验条件' * 8]:
            wrapped = self.ui.wrap_graph_label(label)
            self.assertIn('\n', wrapped)
            self.assertEqual(wrapped.replace('\n', ''), label)

    def test_parallel_reverse_and_self_edges_have_distinct_static_paths(self):
        edges = [{'id': 1, 'source': 1, 'target': 2},
                 {'id': 2, 'source': 2, 'target': 1},
                 {'id': 3, 'source': 1, 'target': 2},
                 {'id': 4, 'source': 1, 'target': 1},
                 {'id': 5, 'source': 1, 'target': 1}]
        curves = self.ui.graph_edge_curves(edges)
        self.assertEqual(curves, self.ui.graph_edge_curves(list(reversed(edges))))
        self.assertEqual(len({json.dumps(curves[i], sort_keys=True) for i in (1, 2, 3)}), 3)
        self.assertNotEqual(curves[4], curves[5])
        self.assertNotIn('dynamic', json.dumps(curves))

    def test_real_html_is_self_contained_and_entity_text_cannot_escape_script(self):
        graph = {'knowledge_base_id': 8,
                 'nodes': [{'id': 1, 'label': '</script><script>alert(1)</script>', 'type': 'Protein'},
                           {'id': 2, 'label': '胞内修饰位点', 'type': 'MicrobialFunction'}],
                 'edges': [{'id': 9, 'source': 1, 'target': 2, 'label': 'MODIFIES'}]}
        with patch.object(self.ui, 'st'), patch.object(self.ui, 'components') as components:
            self.ui.render_graph(graph)
        html = components.html.call_args.args[0]
        self.assertIn('下载白底 PNG', html)
        self.assertIn('litkg-kb-8', html)
        self.assertIn('\\u003c/script\\u003e', html)
        self.assertNotIn('</script><script>alert(1)</script>', html)
        class Resources(HTMLParser):
            remote = []
            def handle_starttag(self, tag, attrs):
                for name, value in attrs:
                    if tag in {'script', 'link', 'img'} and name in {'src', 'href'}:
                        if value.startswith(('http:', 'https:', '//')):
                            self.remote.append(value)
        parser = Resources()
        parser.feed(html)
        self.assertEqual(parser.remote, [])

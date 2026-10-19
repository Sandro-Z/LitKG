"""Exercise graph rendering with the real frontend module and a captured network."""
import importlib.util
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

    def test_single_edge_keeps_compact_predicate_label(self):
        self.assertEqual(self.ui.graph_edge_labels([{'id': 1, 'source': 1, 'target': 2,
            'label': 'ASSOCIATED_WITH', 'qualifiers': {'evidence_type': 'experimental'}}]),
            {1: 'ASSOCIATED_WITH'})

    def test_all_proteomics_types_have_unique_colors_and_legend_uses_current_subgraph(self):
        from app.entity_types import TYPE_DEFINITIONS
        self.assertEqual(set(self.ui.ENTITY_COLORS), set(TYPE_DEFINITIONS))
        self.assertEqual(len(set(self.ui.ENTITY_COLORS.values())), len(TYPE_DEFINITIONS))
        graph = {'nodes': [{'id': 1, 'label': 'A', 'type': 'Proteoform'},
                           {'id': 2, 'label': 'B', 'type': '<unknown>'}],
                 'edges': [{'id': 1, 'source': 1, 'target': 2, 'label': 'AFFECTS'}]}
        network = Mock()
        with patch.object(self.ui, 'Network', return_value=network), patch.object(self.ui, 'st') as st, patch.object(self.ui, 'components'):
            self.ui.render_graph(graph)
        legend = st.markdown.call_args.args[0]
        self.assertIn('Proteoform', legend)
        self.assertIn('&lt;unknown&gt;', legend)
        self.assertNotIn('ProteinGroup', legend)
        self.assertEqual(network.add_node.call_args_list[0].kwargs['color'], self.ui.ENTITY_COLORS['Proteoform'])
        self.assertEqual(network.add_node.call_args_list[1].kwargs['color'], self.ui.ENTITY_COLORS['Other'])

import unittest

from app.graph_diagnostics import connectivity_metrics


class GraphDiagnosticsTests(unittest.TestCase):
    def test_self_loops_and_disconnected_components(self):
        metrics = connectivity_metrics([
            {"source": 1, "target": 2}, {"source": 2, "target": 3},
            {"source": 4, "target": 4}, {"source": 5, "target": 6},
        ])
        self.assertEqual(metrics["node_count"], 6)
        self.assertEqual(metrics["component_count"], 3)
        self.assertEqual(metrics["largest_component_ratio"], 0.5)
        self.assertEqual(metrics["isolated_node_count"], 1)
        self.assertEqual(metrics["self_loop_relation_count"], 1)

    def test_empty_graph_has_finite_metrics(self):
        metrics = connectivity_metrics([])
        self.assertEqual(metrics["node_count"], 0)
        self.assertEqual(metrics["component_count"], 0)
        self.assertEqual(metrics["largest_component_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()

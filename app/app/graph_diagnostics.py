from collections import Counter


def connectivity_metrics(relations) -> dict:
    """Weak connectivity; self-loops do not connect a node to other nodes."""
    parents = {}
    sizes = {}
    self_loops = 0
    relation_count = 0

    def root(node):
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    for relation in relations:
        relation_count += 1
        subject, object_ = relation["source"], relation["target"]
        for node in (subject, object_):
            if node not in parents:
                parents[node] = node
                sizes[node] = 1
        left, right = root(subject), root(object_)
        if subject == object_:
            self_loops += 1
        elif left != right:
            if sizes[left] < sizes[right]:
                left, right = right, left
            parents[right] = left
            sizes[left] += sizes.pop(right)
    histogram = Counter(sizes.values())
    node_count = len(parents)
    largest = max(sizes.values(), default=0)
    return {
        "node_count": node_count,
        "relation_count": relation_count,
        "component_count": len(sizes),
        "largest_component_node_count": largest,
        "largest_component_ratio": largest / node_count if node_count else 0.0,
        "outside_largest_component_node_count": node_count - largest,
        "isolated_node_count": histogram[1],
        "small_component_max_nodes": 5,
        "small_component_node_count": sum(size * count for size, count in histogram.items() if size <= 5),
        "self_loop_relation_count": self_loops,
        "component_size_distribution": [
            {"node_count": size, "component_count": count}
            for size, count in sorted(histogram.items(), reverse=True)
        ],
    }


def fetch_graph_diagnostics(cur, knowledge_base_id: int) -> dict:
    cur.execute(
        """
        SELECT DISTINCT r.id, r.subject_entity_id AS source, r.object_entity_id AS target
        FROM relations r
        JOIN relation_evidence re ON re.relation_id = r.id
        JOIN claims c ON c.id = re.claim_id
        JOIN knowledge_base_documents member ON member.document_id = c.document_id
        WHERE member.knowledge_base_id = %s
        """,
        (knowledge_base_id,),
    )
    return connectivity_metrics(cur.fetchall())

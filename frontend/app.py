import os

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network
from streamlit_autorefresh import st_autorefresh


API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080")

st.set_page_config(
    page_title="LitKG 统一文献知识库",
    page_icon="🧬",
    layout="wide",
)

ENTITY_COLORS = {
    "Compound": "#F97316",
    "Drug": "#FB7185",
    "Protein": "#3B82F6",
    "Gene": "#6366F1",
    "Disease": "#EF4444",
    "CellLine": "#14B8A6",
    "Organism": "#22C55E",
    "Assay": "#EAB308",
    "Pathway": "#8B5CF6",
    "Mutation": "#EC4899",
    "Other": "#94A3B8",
}


def api_get(path: str, params: dict | None = None):
    response = requests.get(
        f"{API_BASE_URL}{path}",
        params=params,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def api_post_json(path: str, payload: dict):
    response = requests.post(
        f"{API_BASE_URL}{path}",
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def api_upload_batch(
    knowledge_base_id: int,
    uploaded_files,
    batch_name: str,
    force_reprocess: bool,
):
    files = [
        (
            "files",
            (file.name, file.getvalue(), "application/pdf"),
        )
        for file in uploaded_files
    ]
    response = requests.post(
        f"{API_BASE_URL}/knowledge-bases/{knowledge_base_id}/documents",
        files=files,
        data={
            "batch_name": batch_name,
            "force_reprocess": str(force_reprocess).lower(),
        },
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


def render_graph(graph_data):
    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])
    if not nodes or not edges:
        st.info("当前筛选范围还没有可展示的关系。")
        return

    network = Network(
        height="760px",
        width="100%",
        directed=True,
        bgcolor="#ffffff",
        font_color="#111827",
    )
    network.barnes_hut(
        gravity=-30000,
        central_gravity=0.3,
        spring_length=180,
        spring_strength=0.04,
        damping=0.09,
    )

    for node in nodes:
        node_type = node.get("type") or "Other"
        normalized_id = node.get("normalized_id") or "未规范化"
        network.add_node(
            node["id"],
            label=node.get("label") or str(node["id"]),
            title=(
                f"类型: {node_type}<br>"
                f"名称: {node.get('label')}<br>"
                f"规范化 ID: {normalized_id}"
            ),
            color=ENTITY_COLORS.get(
                node_type,
                ENTITY_COLORS["Other"],
            ),
            shape="dot",
            size=24,
        )

    for edge in edges:
        confidence = edge.get("confidence")
        confidence_text = (
            "NA" if confidence is None else f"{confidence:.2f}"
        )
        samples = edge.get("evidence_samples") or []
        evidence_html = "<br><br>".join(
            (
                f"[{sample.get('filename')}] "
                f"{sample.get('sentence')}"
            )
            for sample in samples
        )

        edge_color = "#64748B"
        if edge.get("negated"):
            edge_color = "#DC2626"
        elif edge.get("speculative"):
            edge_color = "#D97706"

        network.add_edge(
            edge["source"],
            edge["target"],
            label=edge.get("label", ""),
            title=(
                f"关系: {edge.get('label')}<br>"
                f"平均置信度: {confidence_text}<br>"
                f"支持文献: {edge.get('document_count', 0)}<br>"
                f"证据数: {edge.get('evidence_count', 0)}<br><br>"
                f"{evidence_html}"
            ),
            color=edge_color,
            arrows="to",
            width=min(8, 1 + edge.get("document_count", 1)),
        )

    network.set_options(
        """
        const options = {
          "nodes": {"font": {"size": 16, "face": "Arial"}},
          "edges": {
            "font": {"size": 12, "align": "middle"},
            "smooth": {"type": "dynamic"}
          },
          "physics": {
            "enabled": true,
            "stabilization": {"iterations": 200}
          },
          "interaction": {
            "hover": true,
            "navigationButtons": true,
            "keyboard": true
          }
        }
        """
    )
    components.html(network.generate_html(), height=800, scrolling=True)


def load_knowledge_bases():
    return api_get("/knowledge-bases", params={"limit": 500}).get(
        "knowledge_bases",
        [],
    )


def main():
    st.title("🧬 LitKG 统一文献知识库")
    st.caption(
        "批量摄取 PDF，跨文献合并实体与关系，并保留逐篇、逐句证据来源"
    )

    try:
        knowledge_bases = load_knowledge_bases()
    except Exception as exc:
        st.error(f"无法连接 API：{exc}")
        st.stop()

    with st.sidebar:
        st.header("知识库")
        if not knowledge_bases:
            st.warning("当前没有知识库，请先创建。")
            selected_knowledge_base_id = None
        else:
            labels = {
                item["id"]: (
                    f"{item['name']} · {item['document_count']} 篇文献"
                )
                for item in knowledge_bases
            }
            selected_knowledge_base_id = st.selectbox(
                "当前知识库",
                options=list(labels),
                format_func=lambda item_id: labels[item_id],
            )

        with st.expander("新建知识库"):
            with st.form("create_knowledge_base"):
                new_name = st.text_input("名称")
                new_description = st.text_area("说明")
                create_clicked = st.form_submit_button("创建")
            if create_clicked:
                try:
                    api_post_json(
                        "/knowledge-bases",
                        {
                            "name": new_name,
                            "description": new_description or None,
                        },
                    )
                    st.success("知识库已创建。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"创建失败：{exc}")

        st.divider()
        auto_refresh = st.checkbox("自动刷新", value=True)
        refresh_interval = st.slider(
            "刷新间隔（秒）",
            min_value=3,
            max_value=60,
            value=10,
        )
        if auto_refresh:
            st_autorefresh(
                interval=refresh_interval * 1000,
                key="litkg_autorefresh",
            )
        st.caption(f"API：{API_BASE_URL}")

    if selected_knowledge_base_id is None:
        st.stop()

    tabs = st.tabs(
        [
            "📤 批量导入",
            "🧰 批次进度",
            "📚 文献",
            "🧾 Claims",
            "🕸️ 统一图谱",
        ]
    )

    with tabs[0]:
        st.subheader("批量导入 PDF")
        uploaded_files = st.file_uploader(
            "选择一批 PDF 文献",
            type=["pdf"],
            accept_multiple_files=True,
        )
        batch_name = st.text_input("批次名称（可选）")
        force_reprocess = st.checkbox(
            "重新处理知识库中已有的相同 PDF",
            value=False,
            help="默认按 SHA-256 去重，已处理文献会直接复用。",
        )

        if uploaded_files:
            total_size = sum(len(file.getvalue()) for file in uploaded_files)
            col1, col2 = st.columns(2)
            col1.metric("文献数量", len(uploaded_files))
            col2.metric("总大小", f"{total_size / 1024 / 1024:.1f} MB")

            if st.button("上传并开始批量处理", type="primary"):
                try:
                    result = api_upload_batch(
                        selected_knowledge_base_id,
                        uploaded_files,
                        batch_name,
                        force_reprocess,
                    )
                    st.session_state["last_batch_id"] = result["id"]
                    st.success(
                        f"批次 {result['id']} 已建立，"
                        f"接收 {result['accepted_count']} / "
                        f"{result['submitted_count']} 个文件。"
                    )
                    st.dataframe(
                        pd.DataFrame(result["items"]),
                        use_container_width=True,
                        hide_index=True,
                    )
                except Exception as exc:
                    st.error(f"批量上传失败：{exc}")

    with tabs[1]:
        st.subheader("批次进度")
        try:
            batches = api_get(
                f"/knowledge-bases/{selected_knowledge_base_id}/batches",
                params={"limit": 100},
            ).get("batches", [])

            if not batches:
                st.info("当前知识库还没有导入批次。")
            else:
                st.dataframe(
                    pd.DataFrame(batches),
                    use_container_width=True,
                    hide_index=True,
                )
                default_batch = st.session_state.get(
                    "last_batch_id",
                    batches[0]["id"],
                )
                batch_ids = [batch["id"] for batch in batches]
                selected_batch_id = st.selectbox(
                    "查看批次",
                    batch_ids,
                    index=(
                        batch_ids.index(default_batch)
                        if default_batch in batch_ids
                        else 0
                    ),
                )
                batch = api_get(
                    f"/ingestion-batches/{selected_batch_id}"
                )
                items = batch.get("items", [])
                finished = sum(
                    item["status"] in {"done", "partial", "failed", "rejected"}
                    for item in items
                )
                progress = finished / len(items) if items else 0.0
                col1, col2, col3 = st.columns(3)
                col1.metric("批次状态", batch["status"])
                col2.metric("已接收", batch["accepted_count"])
                col3.metric("完成比例", f"{progress * 100:.1f}%")
                st.progress(progress)
                st.dataframe(
                    pd.DataFrame(items),
                    use_container_width=True,
                    hide_index=True,
                )
        except Exception as exc:
            st.error(f"读取批次失败：{exc}")

    with tabs[2]:
        st.subheader("知识库文献")
        try:
            documents = api_get(
                f"/knowledge-bases/{selected_knowledge_base_id}/documents",
                params={"limit": 500},
            ).get("documents", [])
            if documents:
                st.dataframe(
                    pd.DataFrame(documents),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("暂无文献。")
        except Exception as exc:
            st.error(f"读取文献失败：{exc}")

    with tabs[3]:
        st.subheader("跨文献 Claims")
        col1, col2 = st.columns(2)
        predicate = col1.text_input("按关系筛选（可选）")
        entity_type = col2.selectbox(
            "按实体类型筛选",
            [
                "",
                "Compound",
                "Drug",
                "Protein",
                "Gene",
                "Disease",
                "CellLine",
                "Organism",
                "Assay",
                "Pathway",
                "Mutation",
                "Other",
            ],
        )
        try:
            claims = api_get(
                f"/knowledge-bases/{selected_knowledge_base_id}/claims",
                params={
                    "limit": 500,
                    "predicate": predicate or None,
                    "entity_type": entity_type or None,
                },
            ).get("claims", [])
            if claims:
                st.dataframe(
                    pd.DataFrame(claims),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("当前筛选条件没有 claims。")
        except Exception as exc:
            st.error(f"读取 claims 失败：{exc}")

    with tabs[4]:
        st.subheader("统一、可溯源知识图谱")
        col1, col2, col3 = st.columns(3)
        graph_limit = col1.slider(
            "最多关系数",
            min_value=20,
            max_value=2000,
            value=300,
            step=20,
        )
        min_document_count = col2.number_input(
            "最少支持文献数",
            min_value=1,
            value=1,
            step=1,
        )
        evidence_limit = col3.slider(
            "每条关系显示的证据样本",
            min_value=1,
            max_value=10,
            value=3,
        )
        include_speculative = st.checkbox("包含推测关系", value=True)
        include_negated = st.checkbox("包含否定关系", value=True)

        try:
            graph = api_get(
                f"/knowledge-bases/{selected_knowledge_base_id}/graph",
                params={
                    "limit": graph_limit,
                    "min_document_count": min_document_count,
                    "evidence_limit": evidence_limit,
                    "include_speculative": include_speculative,
                    "include_negated": include_negated,
                },
            )
            summary = graph.get("summary", {})
            col1, col2, col3 = st.columns(3)
            col1.metric("实体", summary.get("node_count", 0))
            col2.metric("聚合关系", summary.get("relation_count", 0))
            col3.metric(
                "覆盖文献",
                summary.get("represented_document_count", 0),
            )
            st.caption(
                "同一实体按类型、规范化 ID 或规范化名称合并；"
                "边粗细代表支持文献数。红色为否定，橙色为推测。"
            )
            render_graph(graph)
        except Exception as exc:
            st.error(f"读取统一图谱失败：{exc}")


if __name__ == "__main__":
    main()

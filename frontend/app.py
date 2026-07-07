import os
import tempfile

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network
from streamlit_autorefresh import st_autorefresh


API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080")


st.set_page_config(
    page_title="LitKG 文献知识抽取系统",
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
    url = f"{API_BASE_URL}{path}"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_upload_pdf(file):
    url = f"{API_BASE_URL}/documents"
    files = {
        "file": (file.name, file.getvalue(), "application/pdf")
    }
    resp = requests.post(url, files=files, timeout=120)
    resp.raise_for_status()
    return resp.json()


def normalize_chunk_stats(chunk_stats):
    stats = {
        "queued": 0,
        "extracting": 0,
        "done": 0,
        "failed": 0,
        "parsing": 0,
    }

    for row in chunk_stats:
        stats[row["status"]] = row["count"]

    total = sum(stats.values())
    done = stats.get("done", 0)
    failed = stats.get("failed", 0)

    if total == 0:
        progress = 0.0
    else:
        progress = (done + failed) / total

    return stats, total, progress


def render_graph(graph_data):
    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])

    if not nodes or not edges:
        st.info("当前文献还没有可展示的图谱关系。")
        return

    net = Network(
        height="720px",
        width="100%",
        directed=True,
        bgcolor="#ffffff",
        font_color="#111827",
    )

    net.barnes_hut(
        gravity=-30000,
        central_gravity=0.3,
        spring_length=180,
        spring_strength=0.04,
        damping=0.09,
    )

    for node in nodes:
        node_type = node.get("type") or "Other"
        color = ENTITY_COLORS.get(node_type, ENTITY_COLORS["Other"])

        net.add_node(
            node["id"],
            label=node.get("label", node["id"]),
            title=f"{node_type}: {node.get('label', node['id'])}",
            color=color,
            shape="dot",
            size=22,
        )

    for edge in edges:
        confidence = edge.get("confidence")
        confidence_text = "NA" if confidence is None else f"{confidence:.2f}"

        edge_color = "#64748B"
        if edge.get("negated"):
            edge_color = "#DC2626"
        elif edge.get("speculative"):
            edge_color = "#D97706"

        title = (
            f"关系: {edge.get('label')}\n"
            f"置信度: {confidence_text}\n"
            f"否定: {edge.get('negated')}\n"
            f"推测: {edge.get('speculative')}\n\n"
            f"证据: {edge.get('evidence')}"
        )

        net.add_edge(
            edge["source"],
            edge["target"],
            label=edge.get("label", ""),
            title=title,
            color=edge_color,
            arrows="to",
        )

    net.set_options(
        """
        const options = {
          "nodes": {
            "font": {
              "size": 16,
              "face": "Arial"
            }
          },
          "edges": {
            "font": {
              "size": 12,
              "align": "middle"
            },
            "smooth": {
              "type": "dynamic"
            }
          },
          "physics": {
            "enabled": true,
            "stabilization": {
              "iterations": 200
            }
          },
          "interaction": {
            "hover": true,
            "navigationButtons": true,
            "keyboard": true
          }
        }
        """
    )

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        net.save_graph(f.name)
        html = open(f.name, "r", encoding="utf-8").read()

    components.html(html, height=760, scrolling=True)


def main():
    st.title("🧬 LitKG 文献知识抽取系统")
    st.caption("PDF 文献上传、后台处理进度监控、结构化 claims 与图谱示意图展示")

    with st.sidebar:
        st.header("系统配置")
        st.write("API 后端：")
        st.code(API_BASE_URL)

        auto_refresh = st.checkbox("自动刷新", value=True)
        refresh_interval = st.slider(
            "刷新间隔，秒",
            min_value=3,
            max_value=60,
            value=10,
            step=1,
        )

        if auto_refresh:
            st_autorefresh(
                interval=refresh_interval * 1000,
                key="litkg_autorefresh",
            )

        if st.button("手动刷新"):
            st.rerun()

    tabs = st.tabs(["📤 文献上传", "📚 文献列表", "📈 处理进度", "🧾 Claims", "🕸️ 图谱示意图"])

    with tabs[0]:
        st.subheader("上传 PDF 文献")

        uploaded_file = st.file_uploader(
            "选择一篇 PDF 文献",
            type=["pdf"],
            accept_multiple_files=False,
        )

        if uploaded_file is not None:
            st.write("文件名：", uploaded_file.name)
            st.write("文件大小：", f"{len(uploaded_file.getvalue()) / 1024 / 1024:.2f} MB")

            if st.button("上传并开始处理", type="primary"):
                try:
                    result = api_upload_pdf(uploaded_file)
                    st.success("上传成功，已加入后台处理队列。")
                    st.json(result)
                except Exception as e:
                    st.error(f"上传失败：{e}")

    with tabs[1]:
        st.subheader("最近文献")

        try:
            data = api_get("/documents", params={"limit": 100})
            docs = data.get("documents", [])

            if not docs:
                st.info("暂无文献。")
            else:
                df = pd.DataFrame(docs)
                st.dataframe(
                    df[
                        [
                            "id",
                            "filename",
                            "title",
                            "status",
                            "chunk_count",
                            "chunk_done_count",
                            "chunk_failed_count",
                            "claim_count",
                            "created_at",
                            "updated_at",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
        except Exception as e:
            st.error(f"读取文献列表失败：{e}")

    with tabs[2]:
        st.subheader("处理进度")

        document_id = st.number_input(
            "输入 document_id",
            min_value=1,
            step=1,
            key="progress_doc_id",
        )

        if st.button("查询进度", key="progress_query"):
            pass

        try:
            data = api_get(f"/documents/{document_id}")
            doc = data["document"]
            stats, total, progress = normalize_chunk_stats(data.get("chunks", []))

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("文档状态", doc["status"])
            col2.metric("Chunk 总数", total)
            col3.metric("Claims 数量", data.get("claim_count", 0))
            col4.metric("完成比例", f"{progress * 100:.1f}%")

            st.progress(progress)

            st.write("文件名：", doc["filename"])
            st.write("标题：", doc.get("title") or "未解析")
            st.write("错误信息：", doc.get("error") or "无")

            st.table(pd.DataFrame([stats]))

        except Exception as e:
            st.warning(f"无法读取 document_id={document_id} 的状态：{e}")

    with tabs[3]:
        st.subheader("结构化 Claims")

        document_id = st.number_input(
            "输入 document_id",
            min_value=1,
            step=1,
            key="claims_doc_id",
        )

        claim_limit = st.slider(
            "最多显示 claims 数量",
            min_value=10,
            max_value=1000,
            value=100,
            step=10,
        )

        try:
            data = api_get(
                f"/documents/{document_id}/claims",
                params={"limit": claim_limit},
            )
            claims = data.get("claims", [])

            if not claims:
                st.info("当前文献还没有抽取到 claims。")
            else:
                df = pd.DataFrame(claims)

                display_cols = [
                    "id",
                    "subject_text",
                    "subject_type",
                    "predicate",
                    "object_text",
                    "object_type",
                    "confidence",
                    "negated",
                    "speculative",
                    "evidence_text",
                ]

                st.dataframe(
                    df[display_cols],
                    use_container_width=True,
                    hide_index=True,
                )

                with st.expander("查看原始 JSON"):
                    st.json(claims)

        except Exception as e:
            st.warning(f"无法读取 claims：{e}")

    with tabs[4]:
        st.subheader("图谱示意图")

        document_id = st.number_input(
            "输入 document_id",
            min_value=1,
            step=1,
            key="graph_doc_id",
        )

        graph_limit = st.slider(
            "最多加载关系数量",
            min_value=20,
            max_value=1000,
            value=300,
            step=20,
        )

        st.caption(
            "节点颜色按实体类型区分；红色边表示 negated=true，橙色边表示 speculative=true。"
        )

        try:
            graph = api_get(
                f"/documents/{document_id}/graph",
                params={"limit": graph_limit},
            )

            col1, col2 = st.columns(2)
            col1.metric("节点数量", len(graph.get("nodes", [])))
            col2.metric("边数量", len(graph.get("edges", [])))

            render_graph(graph)

        except Exception as e:
            st.warning(f"无法生成图谱：{e}")


if __name__ == "__main__":
    main()

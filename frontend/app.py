import os
import json
import unicodedata
from collections import defaultdict
from html import escape
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network
from jinja2 import ChoiceLoader, FileSystemLoader
from streamlit_autorefresh import st_autorefresh


API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8080")

st.set_page_config(
    page_title="LitKG 统一文献知识库",
    page_icon="🧬",
    layout="wide",
)

# Keep a distinct color for every canonical type in app/entity_types.py.
ENTITY_COLORS = {
    "Compound": "#F97316",
    "Drug": "#FB7185",
    "Protein": "#3B82F6",
    "Gene": "#6366F1",
    "Disease": "#EF4444",
    "CellLine": "#14B8A6",
    "CellType": "#115E59",
    "Tissue": "#D4A373",
    "AnatomicalStructure": "#7C3E19",
    "RNA": "#A3E635",
    "MolecularComplex": "#3730A3",
    "BiologicalProcess": "#A16207",
    "Phenotype": "#C026D3",
    "Organism": "#22C55E",
    "Assay": "#EAB308",
    "Pathway": "#8B5CF6",
    "Mutation": "#EC4899",
    "Peptide": "#06B6D4",
    "Proteoform": "#0E7490",
    "ProteinGroup": "#84CC16",
    "ProteinFamily": "#047857",
    "ProteinDomain": "#FACC15",
    "ProteinModification": "#1E3A8A",
    "ModificationSite": "#BE123C",
    "Proteome": "#C4B5FD",
    "SubcellularLocation": "#4D7C0F",
    "Metabolite": "#FDBA74",
    "Intervention": "#0284C7",
    "Biospecimen": "#78716C",
    "StudyGroup": "#475569",
    "ExperimentalModel": "#A78BFA",
    "ClinicalOutcome": "#FDA4AF",
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
        timeout=360,
    )
    response.raise_for_status()
    return response.json()


def api_put_json(path: str, payload: dict):
    response = requests.put(f"{API_BASE_URL}{path}", json=payload, timeout=60)
    response.raise_for_status()
    return response.json()


def api_delete(path: str, params: dict):
    response = requests.delete(f"{API_BASE_URL}{path}", params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def knowledge_base_error(exc):
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        try:
            detail = exc.response.json().get("detail")
            if isinstance(detail, str):
                return detail
        except ValueError:
            pass
    return str(exc)


def create_knowledge_base():
    state = st.session_state
    state.pop("knowledge_base_success", None)
    name = state["new_knowledge_base_name"].strip()
    if not name:
        state["knowledge_base_create_error"] = "请输入知识库名称。"
        return
    try:
        created = api_post_json("/knowledge-bases", {
            "name": name,
            "description": state["new_knowledge_base_description"].strip() or None,
        })
    except Exception as exc:
        state["knowledge_base_create_error"] = f"创建失败：{knowledge_base_error(exc)}"
        return
    # Form callbacks run before the next render, so widget state can be reset
    # safely. The success banner survives both this render and auto-refreshes.
    state["new_knowledge_base_name"] = ""
    state["new_knowledge_base_description"] = ""
    state["knowledge_base_create_open"] = False
    state["selected_knowledge_base_id"] = created["id"]
    state.pop("knowledge_base_create_error", None)
    state["knowledge_base_success"] = f"知识库「{created['name']}」已创建。"


def update_knowledge_base(knowledge_base_id):
    state = st.session_state
    state.pop("knowledge_base_success", None)
    name = state[f"edit_knowledge_base_name_{knowledge_base_id}"].strip()
    if not name:
        state[f"knowledge_base_edit_error_{knowledge_base_id}"] = "请输入知识库名称。"
        return
    try:
        updated = api_put_json(f"/knowledge-bases/{knowledge_base_id}", {
            "name": name,
            "description": state[f"edit_knowledge_base_description_{knowledge_base_id}"].strip() or None,
        })
    except Exception as exc:
        state[f"knowledge_base_edit_error_{knowledge_base_id}"] = f"保存失败：{knowledge_base_error(exc)}"
        return
    state.pop(f"knowledge_base_edit_error_{knowledge_base_id}", None)
    state[f"edit_knowledge_base_name_{knowledge_base_id}"] = updated["name"]
    state[f"edit_knowledge_base_description_{knowledge_base_id}"] = updated.get("description") or ""
    state["knowledge_base_success"] = f"知识库「{updated['name']}」已更新。"


def delete_knowledge_base(knowledge_base_id, name):
    state = st.session_state
    state.pop("knowledge_base_success", None)
    confirmation = state[f"delete_knowledge_base_name_{knowledge_base_id}"]
    if confirmation != name:
        state[f"knowledge_base_delete_error_{knowledge_base_id}"] = "名称不匹配，请输入当前知识库的完整名称。"
        return
    try:
        api_delete(f"/knowledge-bases/{knowledge_base_id}", {"confirm_name": confirmation})
    except Exception as exc:
        state[f"knowledge_base_delete_error_{knowledge_base_id}"] = f"删除失败：{knowledge_base_error(exc)}"
        return
    state.pop("selected_knowledge_base_id", None)
    scoped_keys = {f"{prefix}_{knowledge_base_id}" for prefix in (
        "normalization", "diagnostics", "answer", "maintenance", "edit_knowledge_base_name",
        "edit_knowledge_base_description", "delete_knowledge_base_name",
        "knowledge_base_edit_error", "knowledge_base_delete_error",
    )}
    for key in list(state):
        if key in scoped_keys or key.startswith(f"kg_search_{knowledge_base_id}_"):
            del state[key]
    state["knowledge_base_success"] = f"知识库「{name}」已删除。"


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


def graph_edge_labels(edges):
    """Keep context-specific assertions visible as distinct parallel edges."""
    groups = defaultdict(list)
    for edge in edges:
        groups[(edge['source'], edge['target'], edge.get('predicate') or edge.get('label', ''))].append(edge)
    labels = {}
    for group in groups.values():
        for index, edge in enumerate(sorted(group, key=lambda item: item['id']), 1):
            label = edge.get('label') or edge.get('predicate', '')
            if edge.get('negated'):
                label += ' [否定]'
            if edge.get('speculative'):
                label += ' [推测]'
            if len(group) > 1:
                qualifiers = edge.get('qualifiers') or {}
                context = []
                for key in ('metric', 'direction', 'association_direction', 'comparison_group', 'reference_group', 'evidence_type'):
                    if key in qualifiers:
                        context.append(f'{key}={qualifiers[key]}')
                for process in qualifiers.get('processes', []):
                    context.append(f"{process.get('role')}.{process.get('kind')}={process.get('direction', 'unspecified')}")
                label += f' · 条件 {index}/{len(group)}'
                if context:
                    label += '\n' + '; '.join(context)
            labels[edge['id']] = label
    return labels


def wrap_graph_label(value, width=28):
    """Wrap display text without dropping long identifiers or CJK characters."""
    lines = []
    for paragraph in str(value).split("\n"):
        line = ""
        size = 0
        for char in paragraph:
            char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            if size + char_width > width and line:
                split = line.rfind(" ")
                if split > 0:
                    lines.append(line[:split])
                    line = line[split + 1:]
                else:
                    lines.append(line)
                    line = ""
                size = sum(2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in line)
            line += char
            size += char_width
        lines.append(line)
    return "\n".join(lines)


def graph_edge_curves(edges):
    """Give parallel, reverse and self edges distinct, static exportable paths."""
    groups = defaultdict(list)
    for edge in edges:
        groups[tuple(sorted((edge["source"], edge["target"])))].append(edge)
    curves = {}
    for (source, target), group in groups.items():
        ordered = sorted(group, key=lambda edge: edge["id"])
        for index, edge in enumerate(ordered):
            if source == target:
                curves[edge["id"]] = {"smooth": False, "selfReference": {"size": 65 + index * 45}}
                continue
            lane = index - (len(ordered) - 1) / 2
            if lane == 0:
                curves[edge["id"]] = {"smooth": False}
            else:
                clockwise = (lane > 0) == (edge["source"] == source)
                curves[edge["id"]] = {"smooth": {
                    "enabled": True,
                    "type": "curvedCW" if clockwise else "curvedCCW",
                    "roundness": 0.12 + 0.76 * abs(lane) / ((len(ordered) - 1) / 2 + 1),
                }}
    return curves


def render_graph(graph_data):
    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])
    if not nodes or not edges:
        st.info("当前筛选范围还没有可展示的关系。")
        return

    type_colors = {
        node_type: ENTITY_COLORS.get(node_type, ENTITY_COLORS["Other"])
        for node_type in sorted({node.get("type") or "Other" for node in nodes})
    }
    legend = "".join(
        '<span style="display:inline-flex;align-items:center;gap:6px;'
        'white-space:nowrap;font-size:0.875rem;">'
        '<span aria-hidden="true" style="display:inline-block;width:12px;height:12px;'
        f'border-radius:50%;background:{color};border:1px solid rgba(0,0,0,0.15);"></span>'
        f'{escape(node_type)}</span>'
        for node_type, color in type_colors.items()
    )
    st.caption("节点颜色对应实体类型")
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:8px 16px;margin-bottom:12px;">'
        f'{legend}</div>',
        unsafe_allow_html=True,
    )

    network = Network(
        height="760px",
        width="100%",
        directed=True,
        bgcolor="#ffffff",
        font_color="#111827",
        cdn_resources="in_line",
        heading=f"litkg-kb-{graph_data.get('knowledge_base_id', 'graph')}",
    )

    for node in nodes:
        node_type = node.get("type") or "Other"
        normalized_id = node.get("normalized_id") or "未规范化"
        color = type_colors[node_type]
        # Pale fills keep dark labels readable for every entity type.
        fill = "#" + "".join(f"{round(int(color[i:i + 2], 16) * .14 + 255 * .86):02x}" for i in (1, 3, 5))
        network.add_node(
            node["id"],
            label=wrap_graph_label(node.get("label") or str(node["id"])),
            title=(
                f"类型: {escape(node_type)}<br>"
                f"名称: {escape(node.get('label') or str(node['id']))}<br>"
                f"规范化 ID: {escape(normalized_id)}"
            ),
            color={"border": color, "background": fill,
                   "highlight": {"border": color, "background": fill}},
            entityType=node_type,
            typeColor=color,
            shape="box",
        )

    edge_labels = graph_edge_labels(edges)
    edge_curves = graph_edge_curves(edges)
    if len({(e['source'], e['target'], e.get('predicate') or e.get('label', '')) for e in edges}) < len(edges):
        st.caption('同名平行边代表不同条件或证据标注；边上显示区别，悬停可查看完整限定信息。')

    for edge in edges:
        confidence = edge.get("confidence")
        confidence_text = (
            "NA" if confidence is None else f"{confidence:.2f}"
        )
        samples = edge.get("evidence_samples") or []
        evidence_html = "<br><br>".join(
            (
                f"{escape(sample.get('title') or sample.get('filename') or '')}<br>"
                f"DOI: {escape(sample.get('doi') or '未知')} | "
                f"PDF 页码: {sample.get('page') or '未知'}"
                f"–{sample.get('page_end') or sample.get('page') or '未知'}<br>"
                f"{escape(sample.get('sentence') or '')}"
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
            id=edge['id'],
            label=wrap_graph_label(edge_labels[edge['id']], width=42),
            title=(
                f"关系: {escape(edge.get('label') or edge.get('predicate', ''))}<br>"
                f"否定: {'是' if edge.get('negated') else '否'} | "
                f"推测: {'是' if edge.get('speculative') else '否'}<br>"
                f"平均置信度: {confidence_text}<br>"
                f"支持文献: {edge.get('document_count', 0)}<br>"
                f"证据数: {edge.get('evidence_count', 0)}<br>"
                f"限定信息:<pre>{escape(json.dumps(edge.get('qualifiers') or {}, ensure_ascii=False, indent=2))}</pre>"
                f"{evidence_html}"
            ),
            color=edge_color,
            arrows="to",
            width=min(3, 1 + edge.get("document_count", 1) * .4),
            **edge_curves[edge['id']],
        )

    network.set_options(json.dumps({
        "layout": {"randomSeed": 42, "improvedLayout": False},
        "nodes": {"font": {"size": 16, "face": "Arial", "color": "#111827"},
                  "margin": 12, "borderWidth": 2, "widthConstraint": {"maximum": 230},
                  "shapeProperties": {"borderRadius": 6}},
        "edges": {"font": {"size": 14, "face": "Arial", "align": "horizontal",
                            "color": "#334155", "background": "#ffffff", "strokeWidth": 2,
                            "strokeColor": "#ffffff"},
                  "arrowStrikethrough": False, "smooth": False,
                  "selectionWidth": 0, "hoverWidth": 0},
        "physics": {"enabled": False, "solver": "barnesHut",
                    "barnesHut": {"gravitationalConstant": -18000, "centralGravity": .12,
                                  "springLength": 300, "springConstant": .025,
                                  "damping": .35, "avoidOverlap": 1},
                    "stabilization": {"enabled": True, "iterations": 500, "updateInterval": 25}},
        "interaction": {"hover": True, "keyboard": True, "multiselect": True},
    }))
    # Reuse PyVis's bundled renderer; the custom template needs no CDN requests.
    network.templateEnv.loader = ChoiceLoader([
        FileSystemLoader(str(Path(__file__).parent)), network.templateEnv.loader,
    ])
    network.template = network.templateEnv.get_template("graph.html")
    st.caption("可拖动节点微调，按住 Ctrl 多选。整理后的布局可导出完整白底图片；图片包含当前筛选结果。")
    components.html(network.generate_html(notebook=True), height=920, scrolling=True)


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
    if st.session_state.get("knowledge_base_success"):
        st.success(st.session_state["knowledge_base_success"])

    try:
        knowledge_bases = load_knowledge_bases()
        type_names = list(api_get('/entity-types')['types'])
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
            if st.session_state.get("selected_knowledge_base_id") not in labels:
                st.session_state["selected_knowledge_base_id"] = next(iter(labels))
            selected_knowledge_base_id = st.selectbox(
                "当前知识库",
                options=list(labels),
                format_func=lambda item_id: labels[item_id],
                key="selected_knowledge_base_id",
            )

        if st.button("新建知识库", use_container_width=True):
            st.session_state["knowledge_base_create_open"] = not st.session_state.get("knowledge_base_create_open", False)
            st.session_state.pop("knowledge_base_create_error", None)
        if st.session_state.get("knowledge_base_create_open"):
            with st.form("create_knowledge_base"):
                st.text_input("名称", key="new_knowledge_base_name", max_chars=200)
                st.text_area("说明", key="new_knowledge_base_description", max_chars=2000)
                st.form_submit_button("创建", on_click=create_knowledge_base)
            if st.session_state.get("knowledge_base_create_error"):
                st.error(st.session_state["knowledge_base_create_error"])

        if selected_knowledge_base_id is not None:
            current = next(item for item in knowledge_bases if item["id"] == selected_knowledge_base_id)
            with st.expander("编辑当前知识库"):
                st.session_state.setdefault(f"edit_knowledge_base_name_{selected_knowledge_base_id}", current["name"])
                st.session_state.setdefault(f"edit_knowledge_base_description_{selected_knowledge_base_id}", current.get("description") or "")
                with st.form(f"edit_knowledge_base_{selected_knowledge_base_id}"):
                    st.text_input("知识库名称", max_chars=200,
                                  key=f"edit_knowledge_base_name_{selected_knowledge_base_id}")
                    st.text_area("知识库说明", max_chars=2000,
                                 key=f"edit_knowledge_base_description_{selected_knowledge_base_id}")
                    st.form_submit_button("保存修改", on_click=update_knowledge_base, args=(selected_knowledge_base_id,))
                if st.session_state.get(f"knowledge_base_edit_error_{selected_knowledge_base_id}"):
                    st.error(st.session_state[f"knowledge_base_edit_error_{selected_knowledge_base_id}"])
            with st.expander("删除当前知识库"):
                st.warning(f"将删除「{current['name']}」及其文献、处理缓存、抽取结果、批次、索引和维护记录。其他知识库仍在使用的文献保留；独占文献再次上传将从头处理。原始 PDF 文件保留。此操作无法撤销。")
                with st.form(f"delete_knowledge_base_{selected_knowledge_base_id}"):
                    st.text_input("输入知识库名称以确认删除", key=f"delete_knowledge_base_name_{selected_knowledge_base_id}", max_chars=200)
                    st.form_submit_button("确认删除整个知识库", on_click=delete_knowledge_base,
                                          args=(selected_knowledge_base_id, current["name"]), type="primary")
                if st.session_state.get(f"knowledge_base_delete_error_{selected_knowledge_base_id}"):
                    st.error(st.session_state[f"knowledge_base_delete_error_{selected_knowledge_base_id}"])

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
            "🔎 实体与关系检索",
            "💬 证据问答",
            "📖 知识浏览与维护",
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
                quality = batch.get('processing_quality', {})
                if quality.get('rejected_candidates') or quality.get('pending_reviews'):
                    st.warning(f"结果质量：隔离候选 {quality.get('rejected_candidates', 0)} 个，"
                               f"确认排除 {quality.get('excluded_candidates', 0)} 个，待复核 {quality.get('pending_reviews', 0)} 项。")
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
            [""] + type_names,
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
                    column_order=[
                        "id", "subject_text", "predicate", "object_text",
                        "title", "doi", "page", "page_end", "filename",
                        "section_title", "table_or_figure", "evidence_text",
                        "confidence", "negated", "speculative",
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.info("当前筛选条件没有 claims。")
        except Exception as exc:
            st.error(f"读取 claims 失败：{exc}")

    with tabs[4]:
        st.subheader("统一、可溯源知识图谱")
        normalization_key = f"normalization_{selected_knowledge_base_id}"
        diagnostics_key = f"diagnostics_{selected_knowledge_base_id}"
        with st.expander("实体规范化与完整图谱连通性"):
            st.caption("按原文定义统一缩写和全称，保留数字变体与物种信息。重建使用已有证据，不调用模型。")
            col1, col2 = st.columns(2)
            if col1.button("检查完整知识库连通性"):
                try:
                    st.session_state[diagnostics_key] = api_get(
                        f"/knowledge-bases/{selected_knowledge_base_id}/graph/diagnostics"
                    )
                except Exception as exc:
                    st.error(f"连通性检查失败：{exc}")
            if col2.button("重新规范化已有实体"):
                try:
                    st.session_state[normalization_key] = api_post_json(
                        f"/knowledge-bases/{selected_knowledge_base_id}/rebuild-graph", {}
                    )
                except Exception as exc:
                    st.error(f"启动规范化失败：{exc}")
            job = st.session_state.get(normalization_key)
            if job:
                if job.get("status") in {"queued", "processing"}:
                    try:
                        status_url = f"/knowledge-bases/{selected_knowledge_base_id}/rebuild-graph/{job['task_id']}"
                        updated_job = api_get(status_url)
                        job = {**job, **updated_job}
                        st.session_state[normalization_key] = job
                    except Exception as exc:
                        st.error(f"读取规范化进度失败：{exc}")
                st.caption(f"规范化状态：{job.get('status')}")
                if job.get("error"):
                    st.error(job["error"])
                report = job.get("result")
                if report:
                    st.session_state[diagnostics_key] = report["after"]
                    labels = {
                        "node_count": "实体数", "component_count": "连通分量数",
                        "largest_component_node_count": "主图实体数",
                        "isolated_node_count": "独立实体数",
                    }
                    st.dataframe(pd.DataFrame([
                        {"阶段": stage, **{label: report[key][metric] for metric, label in labels.items()}}
                        for stage, key in [("重建前", "before"), ("重建后", "after")]
                    ]), use_container_width=True, hide_index=True)
                    if report.get("failed_count") or report.get("skipped_active_document_count"):
                        st.warning(
                            f"失败 claim：{report.get('failed_count', 0)}；跳过处理中文献：{report.get('skipped_active_document_count', 0)}。"
                        )
            diagnostics = st.session_state.get(diagnostics_key)
            if diagnostics:
                col1, col2, col3 = st.columns(3)
                col1.metric("完整图谱连通分量", diagnostics["component_count"])
                col2.metric("主图实体占比", f"{diagnostics['largest_component_ratio']:.1%}")
                col3.metric("独立实体", diagnostics["isolated_node_count"])
                st.caption("按完整知识库计算，不受下方显示关系数限制。独立实体包括仅有自环的实体。")
        col1, col2, col3 = st.columns(3)
        graph_query = col1.text_input("搜索实体名称、别名、标准 ID 或关系", key="graph_query")
        graph_predicate = col2.text_input("关系类型（可选）", key="graph_predicate")
        graph_entity_type = col3.selectbox(
            "实体类型（可选）", [""] + type_names, key="graph_entity_type"
        )
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
                    "query": graph_query or None,
                    "predicate": graph_predicate or None,
                    "entity_type": graph_entity_type or None,
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
            st.caption(f"当前显示子图包含 {summary.get('component_count', 0)} 个连通分量；显示数量和筛选条件会影响子图连通性。")
            render_graph(graph)
        except Exception as exc:
            st.error(f"读取统一图谱失败：{exc}")

    with tabs[6]:
        prefix = f'/knowledge-bases/{selected_knowledge_base_id}'
        with st.form('evidence_question'):
            question = st.text_area('向当前知识库提问')
            generate = st.checkbox('根据证据生成答案', value=True)
            submitted_question = st.form_submit_button('检索证据并回答')
        answer_key = f'answer_{selected_knowledge_base_id}'
        if submitted_question:
            try:
                with st.spinner('正在检索原文证据…'):
                    st.session_state[answer_key] = api_post_json(prefix+'/ask', {'question': question, 'generate': generate})
            except Exception as exc:
                st.error(f'问答失败，可重试：{exc}')
        answer = st.session_state.get(answer_key)
        if answer:
            st.write(answer['answer'])
            for warning in answer.get('warnings', []):
                st.warning(warning)
            for ref in answer.get('citations', []):
                with st.expander(f"[{ref['id']}] {ref.get('title') or ref['filename']} · PDF {ref.get('page') or '未知'}"):
                    st.write(ref['sentence'])
                    st.caption(f"DOI: {ref.get('doi') or '未知'} | 文献 {ref['document_id']} | 片段 {ref['chunk_id']} | 否定={ref['negated']} | 推测={ref['speculative']}")
                    st.json(ref['qualifiers'])
                    st.write(ref['context'])

    with tabs[7]:
        prefix = f'/knowledge-bases/{selected_knowledge_base_id}'
        st.caption('摘要为带来源的事实摘录，可随原始记录重建。复核标记不自动修改原始事实。')
        kind = st.selectbox('浏览内容', ['overview','document','entity','fact'],
                            format_func=lambda k: {'overview':'知识库概览','document':'文献摘要','entity':'实体摘要','fact':'完整事实'}[k])
        browse_page = st.number_input('浏览页码', min_value=1, value=1, step=1)
        try:
            pages = api_get(prefix+'/browse', {'kind':kind,'limit':20,'offset':(browse_page-1)*20})['pages']
            if not pages:
                st.info('暂无派生页面，可在下方运行“更新索引和摘要”。')
            for page in pages:
                with st.expander(f"{page['kind']} · {page['source_id']}"):
                    st.text(page['content'])
                    st.json(page['sources'])
        except Exception as exc:
            st.error(f'浏览失败：{exc}')
        with st.expander('目录、处理状态与旧文献升级'):
            doc_id = st.number_input('文献 ID（0 表示维护整个知识库）', min_value=0, value=0, step=1)
            if doc_id and st.button('查看全文实体目录与处理状态'):
                try:
                    processing = api_get(prefix+f'/documents/{doc_id}/processing')
                    quality = processing.get('processing_quality', {})
                    if quality.get('rejected_candidates') or quality.get('pending_reviews'):
                        st.warning(f"已隔离 {quality.get('rejected_candidates', 0)} 个候选，"
                                   f"确认排除 {quality.get('excluded_candidates', 0)} 个，待复核 {quality.get('pending_reviews', 0)} 项。")
                    st.json(processing)
                    st.json(api_get(prefix+f'/documents/{doc_id}/catalog'))
                except Exception as exc:
                    st.error(f'读取失败：{exc}')
            mode = st.selectbox('维护方式', ['derived','reproject','retry','reextract'], format_func=lambda k: {
                'derived':'更新索引和摘要', 'reproject':'重新投影已有记录（无需 LLM）',
                'retry':'重试失败阶段（可能调用 LLM）','reextract':'重新抽取全文（需要 LLM）'}[k])
            st.caption('同一 PDF 可属于多个知识库；更新共享文献会更新所有包含它的知识库。重新投影不能补回旧模型未记录的语义。')
            job_key = f'maintenance_{selected_knowledge_base_id}'
            if st.button('开始维护'):
                try:
                    st.session_state[job_key] = api_post_json(prefix+'/maintenance', {'mode':mode,'document_id':doc_id or None})
                except Exception as exc:
                    st.error(f'启动失败：{exc}')
            if job_key in st.session_state:
                try:
                    st.json(api_get(st.session_state[job_key]['status_url']))
                except Exception as exc:
                    st.error(f'查询失败：{exc}')
        with st.expander('待复核'):
            try:
                review_page = st.number_input('复核页码', min_value=1, value=1, step=1)
                items = api_get(prefix+'/reviews', {'limit':20,'offset':(review_page-1)*20})['reviews']
                if not items:
                    st.info('当前没有待复核项。')
                for item in items:
                    st.write(f"#{item['id']} · 文献 {item['document_id']} · {item['kind']}")
                    st.json(item['detail'])
                    review_outcome = st.selectbox('复核结果', ['resolved', 'dismissed', 'pending'], format_func=lambda value: {'resolved':'修复成功 / 已复核', 'dismissed':'确认无效并排除', 'pending':'仍待解决'}[value], key=f"outcome_{item['id']}")
                    if st.button('保存复核结果', key=f"review_{item['id']}"):
                        api_post_json(prefix+f"/reviews/{item['id']}?status={review_outcome}", {})
                        st.rerun()
            except Exception as exc:
                st.error(f'读取复核项失败：{exc}')

    with tabs[5]:
        st.subheader("检索知识图谱中的实体与关系")
        search_kind = st.radio("检索对象", ["关系", "实体"], horizontal=True)
        result_key = f"kg_search_{selected_knowledge_base_id}_{search_kind}"
        with st.form("kg_search"):
            col1, col2 = st.columns(2)
            query = col1.text_input("关键词", help="支持名称、别名和标准数据库 ID；关系检索也支持关系类型。")
            entity_type = col2.selectbox("实体类型", [""] + list(ENTITY_COLORS), key="search_entity_type")
            params = {"query": query or None, "entity_type": entity_type or None}
            if search_kind == "关系":
                col1, col2, col3 = st.columns(3)
                params["subject"] = col1.text_input("主语名称（可选）") or None
                params["predicate"] = col2.text_input("关系类型（可选）", key="search_predicate") or None
                params["object"] = col3.text_input("宾语名称（可选）") or None
                col1, col2 = st.columns(2)
                selected_entity_id = col1.number_input("实体 ID（0 表示不限）", min_value=0, value=0, step=1)
                params["entity_id"] = selected_entity_id or None
                params["direction"] = col2.selectbox(
                    "相对于该实体的方向",
                    ["both", "outgoing", "incoming"],
                    format_func=lambda value: {"both": "双向", "outgoing": "作为主语", "incoming": "作为宾语"}[value],
                )
                col1, col2 = st.columns(2)
                params["include_negated"] = col1.checkbox("包含否定关系", value=True, key="search_negated")
                params["include_speculative"] = col2.checkbox("包含推测关系", value=True, key="search_speculative")
            col1, col2 = st.columns(2)
            params["limit"] = col1.selectbox("每页条数", [20, 50, 100], index=1)
            page_number = col2.number_input("页数", min_value=1, value=1, step=1)
            params["offset"] = (page_number - 1) * params["limit"]
            submitted = st.form_submit_button("检索", type="primary")

        if submitted:
            if params.get("direction", "both") != "both" and not params.get("entity_id"):
                st.error("按方向筛选时，请填写实体 ID。")
            else:
                try:
                    resource = "relations" if search_kind == "关系" else "entities"
                    st.session_state[result_key] = api_get(
                        f"/knowledge-bases/{selected_knowledge_base_id}/{resource}",
                        params=params,
                    )
                except Exception as exc:
                    st.error(f"检索失败：{exc}")

        result = st.session_state.get(result_key)
        if result is not None:
            if search_kind == "实体":
                entities = result.get("entities", [])
                st.caption(f"本页返回 {len(entities)} 个实体，可使用实体 ID 查询相关关系。")
                if entities:
                    st.dataframe(pd.DataFrame(entities), use_container_width=True, hide_index=True)
                else:
                    st.info("没有匹配的实体。")
            else:
                relations = result.get("relations", [])
                st.caption(f"共匹配 {result.get('total', 0)} 条关系，本页返回 {len(relations)} 条。")
                if not relations:
                    st.info("本页没有匹配的关系。")
                for relation in relations:
                    label = f"{relation['subject']['text']} → {relation['predicate']} → {relation['object']['text']}"
                    with st.expander(label):
                        st.caption(
                            f"支持文献 {relation['document_count']} 篇 · 证据 {relation['evidence_count']} 条 · "
                            f"否定：{'是' if relation['negated'] else '否'} · 推测：{'是' if relation['speculative'] else '否'}"
                        )
                        for sample in relation.get("evidence_samples", []):
                            st.write(sample.get("sentence"))
                            st.caption(
                                f"{sample.get('title') or sample.get('filename')} | DOI: {sample.get('doi') or '未知'} | "
                                f"PDF 页码: {sample.get('page') or '未知'}–{sample.get('page_end') or sample.get('page') or '未知'} | "
                                f"章节: {sample.get('section') or '未知'}"
                            )


if __name__ == "__main__":
    main()

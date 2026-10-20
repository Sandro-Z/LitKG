/* Pure layout and export helpers also run in Node for regression checks. */
const GraphView = (() => {
  const compare = (a, b) => String(a).localeCompare(String(b), "en", {numeric: true});

  function components(nodes, edges) {
    const adjacent = new Map(nodes.map(node => [node.id, new Set()]));
    for (const edge of edges) {
      if (!adjacent.has(edge.from) || !adjacent.has(edge.to)) continue;
      adjacent.get(edge.from).add(edge.to);
      adjacent.get(edge.to).add(edge.from);
    }
    const remaining = new Set([...adjacent.keys()].sort(compare));
    const groups = [];
    while (remaining.size) {
      const queue = [remaining.values().next().value];
      remaining.delete(queue[0]);
      for (let index = 0; index < queue.length; index++) {
        for (const neighbor of adjacent.get(queue[index])) {
          if (remaining.delete(neighbor)) queue.push(neighbor);
        }
      }
      groups.push(queue.sort(compare));
    }
    return groups.sort((a, b) => b.length - a.length || compare(a[0], b[0]));
  }

  function pack(blocks, spacing) {
    const rowWidth = Math.max(1, ...blocks.map(block => block.width),
      Math.sqrt(blocks.reduce((area, block) => area + block.width * block.height, 0)) * 1.3);
    const positions = {};
    let x = 0, y = 0, rowHeight = 0;
    for (const block of blocks) {
      if (x && x + block.width > rowWidth) {
        y += rowHeight + spacing / 2;
        x = 0;
        rowHeight = 0;
      }
      for (const [id, point] of block.positions) {
        positions[id] = {x: point.x - block.left + x, y: point.y - block.top + y};
      }
      x += block.width + spacing / 2;
      rowHeight = Math.max(rowHeight, block.height);
    }
    return positions;
  }

  function packComponents(nodes, edges, positions, boxes, spacing) {
    const blocks = components(nodes, edges).map(ids => {
      const members = new Set(ids);
      const bounds = exportBounds(ids.map(id => boxes[id]), edges.filter(edge => members.has(edge.from)), positions,
        label => ({width: Math.max(...label.split("\n").map(line => line.length * 9)),
          height: label.split("\n").length * 18}));
      return {left: bounds.left, top: bounds.top, positions: new Map(ids.map(id => [id, positions[id]])),
        width: bounds.right - bounds.left, height: bounds.bottom - bounds.top};
    });
    return pack(blocks, spacing);
  }

  function layout(nodes, edges, mode, spacing) {
    const byId = new Map(nodes.map(node => [node.id, node]));
    const outgoing = new Map(nodes.map(node => [node.id, new Set()]));
    const incoming = new Map(nodes.map(node => [node.id, new Set()]));
    for (const edge of edges) {
      if (!byId.has(edge.from) || !byId.has(edge.to) || edge.from === edge.to) continue;
      outgoing.get(edge.from).add(edge.to);
      incoming.get(edge.to).add(edge.from);
    }
    const labelWidth = Math.max(120, ...edges.flatMap(edge => String(edge.label || "").split("\n")
      .map(line => [...line].reduce((width, char) => width + (char.charCodeAt(0) > 255 ? 14 : 8.5), 0))));
    const blocks = components(nodes, edges).map(ids => {
      const positions = new Map();
      // Long node names need extra vertical space even in compact layouts.
      const rowGap = Math.max(spacing * .6,
        ...ids.map(id => String(byId.get(id).label).split("\n").length * 20 + 64));
      if (mode === "circle") {
        const gap = Math.max(spacing, rowGap);
        const radius = ids.length === 1 ? 0 : Math.max(gap, ids.length * gap / (2 * Math.PI));
        ids.forEach((id, index) => {
          const angle = 2 * Math.PI * index / ids.length - Math.PI / 2;
          positions.set(id, {x: radius * Math.cos(angle), y: radius * Math.sin(angle)});
        });
      } else {
        // Directed breadth-first levels also handle cycles and disconnected roots.
        const levels = new Map();
        const queue = ids.filter(id => incoming.get(id).size === 0);
        if (!queue.length) {
          queue.push([...ids].sort((a, b) => outgoing.get(b).size - outgoing.get(a).size || compare(a, b))[0]);
        }
        queue.forEach(id => levels.set(id, 0));
        let cursor = 0;
        while (levels.size < ids.length || cursor < queue.length) {
          if (cursor === queue.length) {
            const next = ids.find(id => !levels.has(id));
            levels.set(next, Math.max(...levels.values()) + 1);
            queue.push(next);
          }
          const id = queue[cursor++];
          for (const target of [...outgoing.get(id)].sort(compare)) {
            if (!levels.has(target)) {
              levels.set(target, levels.get(id) + 1);
              queue.push(target);
            }
          }
        }
        const columns = new Map();
        ids.forEach(id => {
          const level = levels.get(id);
          if (!columns.has(level)) columns.set(level, []);
          columns.get(level).push(id);
        });
        for (const [level, column] of columns) {
          column.forEach((id, index) => positions.set(id, {
            x: level * (spacing + labelWidth),
            y: (index - (column.length - 1) / 2) * rowGap,
          }));
        }
      }
      const points = [...positions.values()];
      const left = Math.min(...points.map(p => p.x)) - 150;
      const top = Math.min(...points.map(p => p.y)) - rowGap / 2;
      return {positions, left, top,
        width: Math.max(...points.map(p => p.x)) - left + 150,
        height: Math.max(...points.map(p => p.y)) - top + rowGap / 2};
    });
    return pack(blocks, spacing);
  }

  function exportBounds(boxes, edges, positions, measureLabel) {
    let left = Infinity, right = -Infinity, top = Infinity, bottom = -Infinity;
    function include(x, y, halfWidth = 0, halfHeight = 0) {
      left = Math.min(left, x - halfWidth);
      right = Math.max(right, x + halfWidth);
      top = Math.min(top, y - halfHeight);
      bottom = Math.max(bottom, y + halfHeight);
    }
    boxes.forEach(box => {
      include(box.left, box.top);
      include(box.right, box.bottom);
    });
    for (const edge of edges) {
      const a = positions[edge.from], b = positions[edge.to];
      if (!a || !b) continue;
      const label = measureLabel(edge.label || "");
      if (edge.from === edge.to) {
        const radius = edge.selfReference?.size || 65;
        // Self loops can extend beyond the node bounding box in either axis.
        include(a.x, a.y, radius * 2 + label.width / 2, radius * 2 + label.height / 2);
      } else {
        const distance = Math.hypot(a.x - b.x, a.y - b.y);
        const bend = edge.smooth ? distance * (edge.smooth.roundness || .5) : 0;
        // Conservatively include the whole curve and its multiline label.
        include((a.x + b.x) / 2, (a.y + b.y) / 2,
          bend + label.width / 2, bend + label.height / 2);
      }
    }
    return {left: left - 40, top: top - 40, right: right + 40, bottom: bottom + 40};
  }

  function imageSize(bounds, ratio, legendRows) {
    const graphWidth = Math.ceil(bounds.right - bounds.left);
    const graphHeight = Math.ceil(bounds.bottom - bounds.top);
    const width = Math.ceil((graphWidth + 80) * ratio);
    const height = Math.ceil((graphHeight + 112 + legendRows * 28) * ratio);
    if (![width, height].every(Number.isFinite) || width < 1 || height < 1 ||
        width > 16000 || height > 16000 || width * height > 48000000) {
      throw new Error("图谱图片过大，请降低清晰度、调小节点间距，或筛选较少的关系后导出。");
    }
    return {width, height, graphWidth, graphHeight};
  }

  return {layout, packComponents, exportBounds, imageSize};
})();

if (typeof module !== "undefined" && module.exports) module.exports = GraphView;

if (typeof document !== "undefined") {
  const nodes = new vis.DataSet(graphData.nodes);
  const edges = new vis.DataSet(graphData.edges);
  const network = new vis.Network(document.getElementById("mynetwork"), {nodes, edges}, graphOptions);
  const controls = Object.fromEntries(["layout", "spacing", "arrange", "freeze", "fit", "resolution", "export", "status"]
    .map(id => [id, document.getElementById(id)]));
  const storageKey = "litkg-layout:" + graphName;
  const signature = JSON.stringify({
    nodes: graphData.nodes.map(node => [node.id, node.label, node.entityType]).sort(),
    edges: graphData.edges.map(edge => [edge.id, edge.from, edge.to, edge.label]).sort(),
  });
  let arranging = false;
  let layoutTimer;

  function status(message, error = false) {
    controls.status.textContent = message;
    controls.status.classList.toggle("error", error);
  }

  function savePositions() {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify({signature,
        positions: network.getPositions(), layout: controls.layout.value, spacing: controls.spacing.value}));
    } catch (_) { /* The graph also works when browser storage is disabled. */ }
  }

  function freeze(message = "布局已固定，可拖动节点微调，或下载完整白底图片。") {
    arranging = false;
    clearTimeout(layoutTimer);
    network.setOptions({physics: {enabled: false}});
    network.stopSimulation();
    controls.arrange.disabled = false;
    savePositions();
    status(message);
  }

  function fit() {
    network.fit({animation: false});
  }

  function finishLayout() {
    if (!arranging) return;
    freeze();
    const positions = GraphView.packComponents(graphData.nodes, graphData.edges, network.getPositions(),
      Object.fromEntries(graphData.nodes.map(node => [node.id, network.getBoundingBox(node.id)])),
      Number(controls.spacing.value));
    nodes.update(graphData.nodes.map(node => ({id: node.id, ...positions[node.id]})));
    network.redraw();
    savePositions();
    fit();
  }
  network.on("stabilizationIterationsDone", finishLayout);
  network.on("stabilized", finishLayout);
  network.on("stabilizationProgress", event => {
    if (arranging) status(`正在整理节点：${Math.round(event.iterations / event.total * 100)}%`);
  });
  network.on("dragStart", () => freeze("拖动节点调整布局，松开后自动保留位置。"));
  network.on("dragEnd", () => { savePositions(); status("节点位置已保留，可继续微调或下载图片。"); });

  function arrange() {
    freeze();
    const spacing = Number(controls.spacing.value);
    if (controls.layout.value === "force") {
      arranging = true;
      controls.arrange.disabled = true;
      status("正在自动整理节点…可点击“固定布局”提前停止。");
      network.setOptions({physics: {enabled: true, barnesHut: {springLength: spacing}}});
      network.stabilize(500);
      // Leave the UI usable even for a graph that does not converge quickly.
      layoutTimer = setTimeout(finishLayout, 20000);
    } else {
      const positions = GraphView.layout(graphData.nodes, graphData.edges, controls.layout.value, spacing);
      nodes.update(graphData.nodes.map(node => ({id: node.id, ...positions[node.id]})));
      network.redraw();
      fit();
      savePositions();
      status("节点已整理并固定，可拖动微调。层次布局中的回边保留原始关系方向。");
    }
  }

  function restorePositions() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey));
      if (saved?.signature !== signature || !graphData.nodes.every(node =>
        Number.isFinite(saved.positions?.[node.id]?.x) && Number.isFinite(saved.positions?.[node.id]?.y))) return false;
      nodes.update(graphData.nodes.map(node => ({id: node.id, ...saved.positions[node.id]})));
      if (["force", "hierarchy", "circle"].includes(saved.layout)) controls.layout.value = saved.layout;
      if (["220", "320", "460"].includes(saved.spacing)) controls.spacing.value = saved.spacing;
      network.redraw();
      fit();
      status("已恢复本标签页中整理的节点位置，可继续调整或导出。");
      return true;
    } catch (_) { return false; }
  }

  function legendLayout(context, availableWidth) {
    const types = new Map(graphData.nodes.map(node => [node.entityType, node.typeColor]));
    const rows = [[]];
    let x = 0;
    context.font = "14px Arial, sans-serif";
    for (const [type, color] of [...types].sort((a, b) => a[0].localeCompare(b[0]))) {
      const width = context.measureText(type).width + 42;
      if (x && x + width > availableWidth) { rows.push([]); x = 0; }
      rows[rows.length - 1].push({type, color, x});
      x += width;
    }
    return rows;
  }

  async function exportImage() {
    freeze();
    const toolbarControls = document.querySelectorAll(".toolbar button, .toolbar select");
    toolbarControls.forEach(control => { control.disabled = true; });
    status("正在生成完整白底图片…");
    let exportNetwork, container, output;
    try {
      await document.fonts.ready;
      const positions = network.getPositions();
      const scratch = document.createElement("canvas").getContext("2d");
      scratch.font = "14px Arial, sans-serif";
      const bounds = GraphView.exportBounds(graphData.nodes.map(node => network.getBoundingBox(node.id)),
        graphData.edges, positions, label => ({
          width: Math.max(...label.split("\n").map(line => scratch.measureText(line).width)),
          height: label.split("\n").length * 18,
        }));
      const rows = legendLayout(scratch, Math.max(320, bounds.right - bounds.left));
      const ratio = Number(controls.resolution.value);
      const size = GraphView.imageSize(bounds, ratio, rows.length);
      const deviceRatio = window.devicePixelRatio || 1;
      container = document.createElement("div");
      container.style.cssText = `position:fixed;left:-100000px;top:0;pointer-events:none;` +
        `width:${size.graphWidth * ratio / deviceRatio}px;height:${size.graphHeight * ratio / deviceRatio}px;`;
      document.body.appendChild(container);
      const options = JSON.parse(JSON.stringify(graphOptions));
      options.physics = {enabled: false};
      options.interaction = {dragNodes: false, dragView: false, zoomView: false, hover: false};
      options.nodes.font.size = 16;
      options.edges.font.size = 14;
      exportNetwork = new vis.Network(container, {
        nodes: new vis.DataSet(graphData.nodes.map(node => ({...node, ...positions[node.id], physics: false}))),
        edges: new vis.DataSet(graphData.edges),
      }, options);
      const source = container.querySelector("canvas");
      const pixelRatio = source.width / parseFloat(container.style.width);
      await new Promise(resolve => {
        exportNetwork.once("afterDrawing", resolve);
        exportNetwork.moveTo({position: {x: (bounds.left + bounds.right) / 2, y: (bounds.top + bounds.bottom) / 2},
          scale: ratio / pixelRatio, animation: false});
        exportNetwork.redraw();
      });
      output = document.createElement("canvas");
      output.width = size.width;
      output.height = size.height;
      const context = output.getContext("2d");
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, size.width, size.height);
      context.scale(ratio, ratio);
      context.fillStyle = "#111827";
      context.font = "bold 22px Arial, sans-serif";
      context.fillText("LitKG 知识图谱", 40, 32);
      context.fillStyle = "#475569";
      context.font = "14px Arial, sans-serif";
      context.fillText(`${graphData.nodes.length} 个节点 · ${graphData.edges.length} 条关系 · 箭头表示关系方向`, 40, 56);
      context.drawImage(source, 40, 72, size.graphWidth, size.graphHeight);
      const legendTop = size.graphHeight + 92;
      rows.forEach((row, index) => row.forEach(item => {
        context.fillStyle = item.color;
        context.fillRect(40 + item.x, legendTop + index * 28, 12, 12);
        context.fillStyle = "#334155";
        context.fillText(item.type, 60 + item.x, legendTop + index * 28 + 12);
      }));
      const blob = await new Promise(resolve => output.toBlob(resolve, "image/png"));
      if (!blob) throw new Error("浏览器无法生成图片，请降低清晰度后重试。");
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${graphName}.png`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      status(`已生成 ${size.width} × ${size.height} 白底 PNG，包含全部 ${graphData.edges.length} 条当前筛选关系。`);
    } catch (error) {
      status(`导出失败：${error.message}`, true);
    } finally {
      if (exportNetwork) exportNetwork.destroy();
      if (container) container.remove();
      if (output) { output.width = 0; output.height = 0; }
      toolbarControls.forEach(control => { control.disabled = false; });
    }
  }

  controls.arrange.addEventListener("click", arrange);
  controls.freeze.addEventListener("click", () => freeze());
  controls.fit.addEventListener("click", fit);
  controls.export.addEventListener("click", exportImage);
  if (!restorePositions()) arrange();
}

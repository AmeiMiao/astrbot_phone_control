/* 同一记忆索引中的向量相似关系。数据正文通过 textContent 显示。 */
(() => {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const colors = { app_experience: "#3b82f6", learning: "#8b5cf6", diary: "#10b981" };
  const make = (tag, attrs = {}) => {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
    return el;
  };
  function render(svg, data, onSelect) {
    const abort = new AbortController();
    const options = { signal: abort.signal };
    const nodes = data.nodes.map((node, i) => ({ ...node,
      x: 500 + (data.nodes.length === 1 ? 0 : 230) * Math.cos(2 * Math.PI * i / data.nodes.length),
      y: 300 + (data.nodes.length === 1 ? 0 : 210) * Math.sin(2 * Math.PI * i / data.nodes.length),
    }));
    const byId = new Map(nodes.map(node => [node.id, node]));
    const edges = data.edges.map(edge => ({ ...edge, a: byId.get(edge.source), b: byId.get(edge.target) }));
    // 有界布局：斥力拉开节点，相似连线将近邻拉近。
    for (let round = 0; round < 65; round += 1) {
      const movement = nodes.map(() => ({ x: 0, y: 0 }));
      for (let i = 0; i < nodes.length; i += 1) for (let j = i + 1; j < nodes.length; j += 1) {
        const dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
        const distance = Math.max(20, Math.hypot(dx, dy));
        const force = 1400 / (distance * distance);
        movement[i].x += dx * force; movement[i].y += dy * force;
        movement[j].x -= dx * force; movement[j].y -= dy * force;
      }
      for (const edge of edges) {
        const i = nodes.indexOf(edge.a), j = nodes.indexOf(edge.b);
        const dx = edge.b.x - edge.a.x, dy = edge.b.y - edge.a.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const force = (distance - 100) * 0.02 * edge.score;
        movement[i].x += dx / distance * force; movement[i].y += dy / distance * force;
        movement[j].x -= dx / distance * force; movement[j].y -= dy / distance * force;
      }
      nodes.forEach((node, i) => {
        node.x = Math.max(55, Math.min(945, node.x + Math.max(-12, Math.min(12, movement[i].x))));
        node.y = Math.max(40, Math.min(550, node.y + Math.max(-12, Math.min(12, movement[i].y))));
      });
    }
    svg.replaceChildren();
    const group = make("g"); svg.append(group);
    const edgeElements = edges.map(edge => {
      const line = make("line", { stroke: "#94a3b8", "stroke-width": 1 + edge.score * 2, "stroke-opacity": 0.65, "pointer-events": "none" });
      const title = make("title"); title.textContent = `余弦相似度 ${edge.score.toFixed(3)}`; line.append(title); group.append(line); return line;
    });
    let selected = null, dragging = null, panning = null;
    const view = { scale: 1, x: 0, y: 0 };
    const circles = new Map(), labels = new Map();
    function select(node) {
      selected = node.id;
      circles.forEach((circle, id) => {
        circle.setAttribute("aria-pressed", String(id === selected));
        circle.setAttribute("stroke", id === selected ? "#f59e0b" : "#fff");
        circle.setAttribute("stroke-width", id === selected ? "5" : "2");
      });
      onSelect(node);
    }
    function draw() {
      group.setAttribute("transform", `translate(${view.x} ${view.y}) scale(${view.scale})`);
      nodes.forEach(node => {
        const circle = circles.get(node.id), label = labels.get(node.id);
        circle.setAttribute("cx", node.x); circle.setAttribute("cy", node.y);
        label.setAttribute("x", node.x); label.setAttribute("y", node.y + 29);
      });
      edges.forEach((edge, i) => {
        const line = edgeElements[i];
        line.setAttribute("x1", edge.a.x); line.setAttribute("y1", edge.a.y);
        line.setAttribute("x2", edge.b.x); line.setAttribute("y2", edge.b.y);
      });
    }
    function point(event) {
      const p = svg.createSVGPoint(); p.x = event.clientX; p.y = event.clientY;
      return p.matrixTransform(svg.getScreenCTM().inverse());
    }
    function zoom(factor, anchor = { x: 500, y: 300 }) {
      const scale = Math.max(0.4, Math.min(3.5, view.scale * factor));
      view.x = anchor.x - (anchor.x - view.x) * scale / view.scale;
      view.y = anchor.y - (anchor.y - view.y) * scale / view.scale;
      view.scale = scale; draw();
    }
    nodes.forEach(node => {
      const circle = make("circle", { r: 15, fill: colors[node.source] || "#64748b", tabindex: 0, role: "button", "aria-label": node.label, "aria-pressed": "false", "data-node-id": node.id });
      const title = make("title"); title.textContent = node.label; circle.append(title);
      const label = make("text", { "text-anchor": "middle", "font-size": 12, fill: "currentColor", "pointer-events": "none" });
      label.textContent = node.label.length > 22 ? node.label.slice(0, 22) + "…" : node.label;
      circle.addEventListener("pointerdown", event => {
        event.stopPropagation(); dragging = node; circle.setPointerCapture(event.pointerId); select(node);
      }, options);
      circle.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(node); }
      }, options);
      circles.set(node.id, circle); labels.set(node.id, label); group.append(circle, label);
    });
    svg.addEventListener("pointerdown", event => { panning = point(event); svg.setPointerCapture(event.pointerId); }, options);
    svg.addEventListener("pointermove", event => {
      const p = point(event);
      if (dragging) { dragging.x = (p.x - view.x) / view.scale; dragging.y = (p.y - view.y) / view.scale; draw(); }
      else if (panning) { view.x += p.x - panning.x; view.y += p.y - panning.y; panning = p; draw(); }
    }, options);
    const finish = () => { dragging = null; panning = null; };
    svg.addEventListener("pointerup", finish, options); svg.addEventListener("pointercancel", finish, options);
    svg.addEventListener("wheel", event => { event.preventDefault(); zoom(event.deltaY < 0 ? 1.1 : 1 / 1.1, point(event)); }, { ...options, passive: false });
    draw(); select(nodes[0]);
    return { zoom, reset: () => { view.scale = 1; view.x = 0; view.y = 0; draw(); }, destroy: () => abort.abort() };
  }
  window.PhoneMemoryGraph = { render };
})();

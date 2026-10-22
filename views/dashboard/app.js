/* 手机操控插件 · 监控与资料管理 */
(() => {
  "use strict";

  const POLL_OVERVIEW_MS = 2000;
  const POLL_SERIES_MS = 2000;
  const SERIES_MINUTES = 30;

  const state = {
    bridge: null,
    view: "dashboard",
    overview: null,
    series: { screenshots: [], llm: [], buckets: [] },
    timers: [],
    imagesOffset: 0,
    imagesRequest: 0,
    migrationToken: null,
  };

  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function fmtNum(value, digits = 0) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const num = Number(value);
    if (digits > 0) return num.toFixed(digits);
    if (Math.abs(num) >= 10000) return `${(num / 1000).toFixed(1)}k`;
    return String(Math.round(num));
  }

  function fmtTime(ts, withSeconds = true) {
    if (!ts) return "—";
    const ms = Number(ts) > 1e12 ? Number(ts) : Number(ts) * 1000;
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return "—";
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    return withSeconds ? `${hh}:${mm}:${ss}` : `${hh}:${mm}`;
  }

  function fmtDateTime(ts) {
    if (!ts) return "—";
    const ms = Number(ts) > 1e12 ? Number(ts) : Number(ts) * 1000;
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return "—";
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
  }

  function ageLabel(ts) {
    if (!ts) return "—";
    const ms = Number(ts) > 1e12 ? Number(ts) : Number(ts) * 1000;
    const age = Math.max(0, Date.now() - ms);
    if (age < 1000) return `${Math.round(age)}ms 前`;
    if (age < 60000) return `${(age / 1000).toFixed(1)}s 前`;
    return `${Math.round(age / 60000)}min 前`;
  }

  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text;
  }

  function setClass(el, names) {
    if (!el) return;
    el.classList.remove("ok", "warn", "error");
    if (names) el.classList.add(names);
  }

  async function getBridge() {
    for (let i = 0; i < 120; i += 1) {
      if (window.AstrBotPluginView) return window.AstrBotPluginView;
      await sleep(50);
    }
    throw new Error("AstrBot 插件页面 bridge 未就绪");
  }

  async function apiGet(endpoint, params) {
    return state.bridge.apiGet(endpoint, params);
  }

  // ---------------- 视图初始化 ----------------
  function switchView(view) {
    state.view = view;
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === view);
    });
    $("view-dashboard").hidden = view !== "dashboard";
    $("view-learning").hidden = view !== "learning";
    $("view-images").hidden = view !== "images";
    $("view-migration").hidden = view !== "migration";
    if (view === "dashboard") {
      schedulePolls();
      refreshOverview();
      refreshSeries();
    } else {
      stopPolls();
      if (view === "learning") loadLearning();
      if (view === "images") loadImages();
    }
  }

  function schedulePolls() {
    stopPolls();
    state.timers.push(setInterval(refreshOverview, POLL_OVERVIEW_MS));
    state.timers.push(setInterval(refreshSeries, POLL_SERIES_MS));
  }

  function stopPolls() {
    state.timers.forEach((timer) => clearInterval(timer));
    state.timers = [];
  }

  // ---------------- 概览 ----------------
  async function refreshOverview() {
    if (document.hidden) return;
    try {
      const data = await apiGet("dashboard/overview");
      if (!data) return;
      state.overview = data;
      renderOverview(data);
    } catch (err) {
      setText("page-subtitle", `概览加载失败：${err.message || err}`);
    }
  }

  function renderOverview(data) {
    const backends = data.backends || {};
    const control = backends.control || "—";
    const controlConfig = backends.control_config || "";
    const scrcpyRunning = !!backends.scrcpy_running;
    // 页面打开后后端会后台懒启动 scrcpy：启动前显示配置值（auto），避免误以为默认只用 ADB。
    setText("ov-control", scrcpyRunning ? "scrcpy" : controlConfig === "adb" ? "adb" : controlConfig || control);
    setClass($("ov-control"), control === "scrcpy" ? "ok" : "");
    setText(
      "ov-control-note",
      scrcpyRunning
        ? "scrcpy 已连接（注入 ≈21ms）"
        : controlConfig === "adb"
          ? "仅 ADB"
          : "scrcpy 懒启动 · 失败回退 ADB",
    );

    const perception = backends.perception || "—";
    setText("ov-perception", perception);
    let perceptionNote;
    if (backends.scrcpy_video === true) {
      perceptionNote = "scrcpy 视频帧可用";
    } else if (perception === "adb") {
      perceptionNote = scrcpyRunning ? "ADB 截图（scrcpy 仅控制通道）" : "ADB 截图来源";
    } else if (scrcpyRunning) {
      perceptionNote = backends.reason || "视频帧不可用，回退 ADB";
    } else {
      perceptionNote = "未启动";
    }
    setText("ov-perception-note", perceptionNote);

    const quota = data.quota || {};
    if (quota.enabled === false) {
      setText("ov-quota", "未启用");
      setText("ov-quota-note", "quota.enabled=false");
    } else {
      const used = Number(quota.used || 0);
      const max = Number(quota.max_tokens || 0);
      const pct = max > 0 ? Math.min(100, Math.round((used / max) * 100)) : 0;
      setText("ov-quota", `${pct}%`);
      setClass($("ov-quota"), pct >= 90 ? "error" : pct >= 70 ? "warn" : "ok");
      setText(
        "ov-quota-note",
        `${fmtNum(used)} / ${fmtNum(max)} · ${quota.window_hours || 24}h 窗口`,
      );
    }

    const shot = data.last_screenshot;
    if (shot) {
      setText("ov-shot", `${fmtNum(shot.ms, 1)} ms`);
      setText(
        "ov-shot-note",
        `${shot.source || "?"} · ${fmtTime(shot.ts)} · ${ageLabel(shot.ts)}`,
      );
    } else {
      setText("ov-shot", "—");
      setText("ov-shot-note", "暂无截图");
    }
    if (shot) updateShotLatest(shot);

    const llm = data.last_llm;
    if (llm) {
      setText("ov-llm", `${fmtNum(llm.tps, 1)} tok/s`);
      setText(
        "ov-llm-note",
        `${fmtNum(llm.tokens)} tok / ${fmtNum(llm.ms, 0)}ms · ${ageLabel(llm.ts)}`,
      );
      updateTpsLatest(llm);
    } else {
      setText("ov-llm", "—");
      setText("ov-llm-note", "暂无 API 调用");
    }

    const traffic = data.traffic || {};
    if (traffic.running) {
      setText("ov-traffic", `${fmtNum(traffic.flows || 0)} 条`);
      setText(
        "ov-traffic-note",
        `运行中 · 端口 ${traffic.port || "?"}`,
      );
      setClass($("ov-traffic"), "ok");
    } else {
      setText("ov-traffic", traffic.enabled ? "未运行" : "未启用");
      setText(
        "ov-traffic-note",
        traffic.enabled
          ? traffic.error || "traffic.enabled=true，等待启动"
          : "traffic.enabled=false",
      );
      setClass($("ov-traffic"), "warn");
    }
    const previewEl = $("ov-traffic-preview");
    if (previewEl) {
      previewEl.textContent =
        traffic.preview || "暂无抓包流量（确认 App 已重启并产生请求）";
    }

    const today = data.today || {};
    setText(
      "ov-interactions",
      `${today.comments || 0} / ${today.passive_replies || 0} / ${today.posts || 0}`,
    );
    setText("ov-interactions-note", "今日主动评论 / 被动回复 / 发帖");

    setText(
      "page-subtitle",
      `插件 v${data.version || "?"} · 运行 ${formatUptime(data.uptime_s)}`,
    );
  }

  function formatUptime(seconds) {
    const s = Number(seconds || 0);
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}min`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  // ---------------- 曲线 ----------------
  async function refreshSeries() {
    if (document.hidden || state.view !== "dashboard") return;
    try {
      const data = await apiGet("dashboard/series", { minutes: SERIES_MINUTES });
      if (!data) return;
      state.series = {
        screenshots: Array.isArray(data.screenshots) ? data.screenshots : [],
        llm: Array.isArray(data.llm) ? data.llm : [],
        buckets: Array.isArray(data.buckets) ? data.buckets : [],
      };
      renderCharts();
      if (data.last_screenshot) updateShotLatest(data.last_screenshot);
      if (data.last_llm) updateTpsLatest(data.last_llm);
    } catch (err) {
      setText("shot-foot", `曲线加载失败：${err.message || err}`);
    }
  }

  function updateShotLatest(shot) {
    setText("shot-latest", `${fmtNum(shot.ms, 1)} ms`);
    setText(
      "shot-foot",
      `最近 30 分钟 · 单位 ms · ${shot.source || "?"} ${fmtTime(shot.ts, false)}`,
    );
  }

  function updateTpsLatest(llm) {
    setText("tps-latest", `${fmtNum(llm.tps, 1)} tok/s`);
  }

  function renderCharts() {
    const now = Date.now() / 1000;
    const from = now - SERIES_MINUTES * 60;

    const shotPoints = state.series.screenshots
      .map((item) => ({ ts: Number(item.ts), value: Number(item.ms) }))
      .filter((item) => Number.isFinite(item.ts) && Number.isFinite(item.value));
    drawChart($("chart-shot"), shotPoints, {
      color: "#3b82f6",
      from: Math.min(from, shotPoints.length ? shotPoints[0].ts : from),
      to: now,
      unit: "ms",
    });

    const bucketPoints = state.series.buckets
      .map((item) => ({ ts: Number(item.ts), value: Number(item.tps) }))
      .filter((item) => Number.isFinite(item.ts) && Number.isFinite(item.value));
    const llmPoints = state.series.llm
      .map((item) => ({ ts: Number(item.ts), value: Number(item.tps) }))
      .filter((item) => Number.isFinite(item.ts) && Number.isFinite(item.value));
    const tpsPoints = bucketPoints.length ? bucketPoints : llmPoints;
    drawChart($("chart-tps"), tpsPoints, {
      color: "#8b5cf6",
      from: Math.min(from, tpsPoints.length ? tpsPoints[0].ts : from),
      to: now,
      unit: "tok/s",
    });
  }

  function niceMax(value) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(value)));
    const normalized = value / pow;
    const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return step * pow;
  }

  function drawChart(canvas, points, opts) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(220, Math.floor(rect.width || canvas.parentElement.clientWidth || 300));
    const height = Math.max(140, Math.floor(rect.height || 180));
    if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const pad = { left: 42, right: 10, top: 12, bottom: 22 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const styles = getComputedStyle(document.documentElement);
    const textColor = styles.getPropertyValue("--muted").trim() || "#6b7280";
    const borderColor = styles.getPropertyValue("--border").trim() || "#e5e7eb";

    const from = opts.from || opts.to - SERIES_MINUTES * 60;
    const to = opts.to || Date.now() / 1000;
    const xOf = (ts) => pad.left + ((ts - from) / Math.max(1, to - from)) * plotW;

    let maxVal = 1;
    points.forEach((p) => {
      maxVal = Math.max(maxVal, p.value);
    });
    maxVal = niceMax(maxVal * 1.15);
    const yOf = (v) => pad.top + plotH - (v / maxVal) * plotH;

    // 网格 + 纵轴
    ctx.strokeStyle = borderColor;
    ctx.globalAlpha = 0.7;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const v = (maxVal / 4) * i;
      const y = yOf(v);
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = textColor;
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(fmtNum(v, v < 10 ? 1 : 0), pad.left - 6, y);
    }
    // 横轴时间
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let i = 0; i <= 3; i += 1) {
      const ts = from + ((to - from) / 3) * i;
      ctx.fillText(fmtTime(ts, false), xOf(ts), height - pad.bottom + 6);
    }

    if (!points.length) {
      ctx.fillStyle = textColor;
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("暂无数据", width / 2, height / 2);
      return;
    }

    const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
    gradient.addColorStop(0, `${opts.color}55`);
    gradient.addColorStop(1, `${opts.color}05`);
    ctx.beginPath();
    points.forEach((p, idx) => {
      const x = xOf(p.ts);
      const y = yOf(p.value);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(xOf(points[points.length - 1].ts), pad.top + plotH);
    ctx.lineTo(xOf(points[0].ts), pad.top + plotH);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    points.forEach((p, idx) => {
      const x = xOf(p.ts);
      const y = yOf(p.value);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = opts.color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.stroke();

    const last = points[points.length - 1];
    ctx.beginPath();
    ctx.arc(xOf(last.ts), yOf(last.value), 3, 0, Math.PI * 2);
    ctx.fillStyle = opts.color;
    ctx.fill();
  }

  function redrawCharts() {
    if (state.view === "dashboard") renderCharts();
  }

  // ---------------- 学习记录 ----------------
  async function loadLearning() {
    if (document.hidden) return;
    const query = ($("learning-query")?.value || "").trim();
    const limit = Number($("learning-limit")?.value || 50);
    try {
      const data = await apiGet("dashboard/learning", { query, limit });
      renderLearning(data || {});
    } catch (err) {
      setText(
        "learning-subtitle",
        `学习记录加载失败：${err.message || err}`
      );
    }
  }

  function renderLearning(data) {
    const stats = data.stats || {};
    const entries = data.entries || [];
    const total = Number(stats.entries || 0);
    const maxEntries = Number(stats.max_entries || 0);
    const last = Number(stats.last_ts || 0);
    const sources = Object.entries(stats.sources || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([name, count]) => `${name} ${count}`)
      .join(" · ");
    const query = String(data.query || "").trim();
    setText(
      "learning-subtitle",
      total
        ? `共 ${total} 条${maxEntries ? `（上限 ${maxEntries}）` : ""}；最近记录 ${fmtDateTime(last)}${query ? `；搜索「${query}」` : ""}`
        : "学习档案还是空的：持续刷手机/自主浏览时 AI 会用 phone_learn 记录理解到的内容。"
    );
    const statBox = $("learning-stats");
    if (statBox) {
      statBox.innerHTML = "";
      const chips = [
        `总数 ${total}`,
        maxEntries ? `上限 ${maxEntries}` : "",
        last ? `最近 ${fmtDateTime(last)}` : "",
        sources ? `来源 ${sources}` : "",
      ].filter(Boolean);
      chips.forEach((text) => {
        const chip = document.createElement("span");
        chip.className = "learning-chip";
        chip.textContent = text;
        statBox.appendChild(chip);
      });
    }
    const list = $("learning-list");
    if (!list) return;
    list.innerHTML = "";
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "learning-empty";
      empty.textContent = query
        ? "没有匹配的学习记录，换个关键词试试。"
        : "还没有学习记录。";
      list.appendChild(empty);
      return;
    }
    entries.forEach((entry) => {
      const item = document.createElement("article");
      item.className = "learning-item";

      const head = document.createElement("div");
      head.className = "learning-item-head";
      const time = document.createElement("span");
      time.className = "learning-time";
      time.textContent = fmtDateTime(entry.ts);
      head.appendChild(time);
      const source = document.createElement("span");
      source.className = "learning-source";
      source.textContent = entry.source || "未标注来源";
      head.appendChild(source);
      item.appendChild(head);

      const content = document.createElement("p");
      content.className = "learning-content";
      content.textContent = entry.content || "";
      item.appendChild(content);

      const tags = entry.tags || [];
      if (tags.length) {
        const tagBox = document.createElement("div");
        tagBox.className = "learning-tags";
        tags.forEach((tag) => {
          const tagEl = document.createElement("span");
          tagEl.textContent = `#${tag}`;
          tagBox.appendChild(tagEl);
        });
        item.appendChild(tagBox);
      }
      const remove = button("删除记录", async () => {
        if (!await confirmAction("删除这条本地学习记录？已同步到记忆插件的副本由记忆插件管理。")) return;
        await state.bridge.apiPost("dashboard/learning/delete", { id: entry.id });
        await loadLearning();
      });
      item.appendChild(remove);
      list.appendChild(item);
    });
  }

  function button(label, action) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "btn ghost";
    el.textContent = label;
    el.addEventListener("click", async () => {
      el.disabled = true;
      $("library-error").hidden = true;
      try { await action(); }
      catch (err) {
        setText("library-error", err.message || String(err));
        $("library-error").hidden = false;
        $("library-error").focus();
      }
      finally { el.disabled = false; }
    });
    return el;
  }

  function confirmAction(message, confirmLabel = "确认删除") {
    return new Promise(resolve => {
      const previous = document.activeElement;
      const dialog = document.createElement("dialog");
      dialog.className = "confirm-dialog";
      dialog.setAttribute("aria-label", confirmLabel);
      const text = document.createElement("p");
      text.textContent = message;
      const actions = document.createElement("div");
      actions.className = "head-actions";
      const finish = value => {
        dialog.close(); dialog.remove();
        if (previous?.isConnected) previous.focus();
        resolve(value);
      };
      actions.append(button("取消", () => finish(false)), button(confirmLabel, () => finish(true)));
      dialog.append(text, actions);
      dialog.addEventListener("cancel", event => { event.preventDefault(); finish(false); });
      document.body.appendChild(dialog);
      dialog.showModal();
      actions.firstElementChild.focus();
    });
  }

  // ---------------- 图片库 ----------------
  async function loadImages() {
    const requestId = ++state.imagesRequest;
    try {
      const data = await apiGet("dashboard/images", {
        query: $("images-query").value.trim(), offset: state.imagesOffset,
      });
      if (requestId !== state.imagesRequest) return;
      if (!data.entries.length && data.total && state.imagesOffset) {
        state.imagesOffset = Math.floor((data.total - 1) / 30) * 30;
        return loadImages();
      }
      setText("images-subtitle", `共 ${data.total} 张图片`);
      setText("images-page", `${data.total ? Math.floor(data.offset / 30) + 1 : 0} / ${Math.ceil(data.total / 30)}`);
      $("images-prev").disabled = data.offset === 0;
      $("images-next").disabled = data.offset + data.entries.length >= data.total;
      const list = $("images-list");
      list.replaceChildren();
      if (!data.entries.length) {
        const empty = document.createElement("p");
        empty.className = "learning-empty";
        empty.textContent = data.query ? "没有匹配的图片。" : "还没有保存的图片。";
        list.appendChild(empty);
      }
      data.entries.forEach((entry) => renderImage(entry, list));
    } catch (err) { setText("images-subtitle", `图片加载失败：${err.message || err}`); }
  }

  function renderImage(entry, list) {
    const item = document.createElement("article");
    item.className = "learning-item image-item";
    const visual = document.createElement("div");
    visual.className = "image-preview";
    visual.textContent = entry.server_path ? "点击查看图片" : "原图保存在手机，查看时需连接对应设备";
    if (entry.server_path || entry.phone_path) {
      visual.appendChild(button("查看图片", async () => {
        const data = await apiGet("dashboard/images/preview", { id: entry.id });
        const img = document.createElement("img");
        img.src = data.url;
        img.alt = entry.description || "保存的图片";
        const full = document.createElement("a");
        full.href = data.url;
        full.download = `${entry.id}.${entry.format || "png"}`;
        full.textContent = "下载原图";
        visual.replaceChildren(img, full);
      }));
    }
    item.appendChild(visual);
    const form = document.createElement("form");
    const fields = {};
    function field(key, label, value, multiline = false) {
      const wrapper = document.createElement("label");
      wrapper.textContent = label;
      const input = document.createElement(multiline ? "textarea" : "input");
      input.value = value || "";
      input.name = key;
      input.maxLength = key === "tags" ? 409 : 500;
      input.disabled = true;
      wrapper.appendChild(input);
      form.appendChild(wrapper);
      fields[key] = input;
    }
    field("description", "图片语义", entry.description, true);
    field("reason", "保存理由", entry.reason, true);
    field("tags", "标签（逗号分隔，最多 10 个）", (entry.tags || []).join(", "));
    const kindLabel = document.createElement("label");
    kindLabel.textContent = "类型";
    const kind = document.createElement("select");
    for (const [value, label] of [["image", "图片"], ["sticker", "表情包"]]) {
      const option = document.createElement("option");
      option.value = value; option.textContent = label; kind.appendChild(option);
    }
    kind.value = entry.kind || "image"; kind.disabled = true;
    kindLabel.appendChild(kind); form.appendChild(kindLabel); fields.kind = kind;
    const source = document.createElement("p");
    source.className = "image-meta";
    source.textContent = `保存于 ${fmtDateTime(entry.created_at)} · 来源 ${(entry.sources || []).map(s => s.app + (s.content_id ? " / " + s.content_id : "")).join("、") || "未标注"}\n服务端：${entry.server_path || "未保存"}\n手机：${entry.phone_path || "未保存"}${entry.last_error ? "\n保存错误：" + entry.last_error : ""}`;
    form.appendChild(source);
    const actions = document.createElement("div");
    actions.className = "head-actions";
    const edit = button("编辑语义", () => {
      Object.values(fields).forEach(f => { f.disabled = false; });
      edit.hidden = true; save.hidden = false; cancel.hidden = false;
      fields.description.focus();
    });
    const save = button("保存语义", async () => {
      await state.bridge.apiPost("dashboard/images/update", {
        id: entry.id, description: fields.description.value, reason: fields.reason.value,
        tags: fields.tags.value.split(/[,，]/).map(t => t.trim()).filter(Boolean), kind: kind.value,
      });
      await loadImages();
    });
    const cancel = button("取消", () => {
      fields.description.value = entry.description || "";
      fields.reason.value = entry.reason || "";
      fields.tags.value = (entry.tags || []).join(", "); kind.value = entry.kind || "image";
      Object.values(fields).forEach(f => { f.disabled = true; });
      edit.hidden = false; save.hidden = true; cancel.hidden = true;
    });
    const remove = button("删除图片", async () => {
      if (!await confirmAction("删除图片库记录与服务端原图？此操作不可恢复，手机相册副本保留。")) return;
      await state.bridge.apiPost("dashboard/images/delete", { id: entry.id });
      await loadImages();
    });
    save.hidden = true; cancel.hidden = true;
    actions.append(edit, save, cancel, remove); form.appendChild(actions);
    form.addEventListener("submit", e => e.preventDefault());
    item.appendChild(form); list.appendChild(item);
  }

  // ---------------- 初始化 ----------------
  async function previewMigration() {
    state.migrationToken = null;
    $("btn-migration-apply").disabled = true;
    setText("migration-fields", "");
    try {
      const plan = await apiGet("dashboard/config-migration");
      state.migrationToken = plan.token;
      $("btn-migration-apply").disabled = plan.count === 0;
      setText("migration-status", `待导入 ${plan.count} 项，保留当前自定义设置 ${plan.preserved} 项。`);
      setText("migration-fields", `待导入：\n${plan.changed.join("\n") || "无"}\n\n保留当前值：\n${plan.kept.join("\n") || "无"}`);
    } catch (err) {
      setText("migration-status", `预览失败：${err.message || err}`);
    }
  }

  async function applyMigration() {
    if (!state.migrationToken) return;
    if (!await confirmAction("导入预览中的原插件配置？自定义设置保持当前值，原插件文件保持完整。导入后将按新配置连接设备并调整后台任务。", "确认导入")) return;
    $("btn-migration-apply").disabled = true;
    try {
      const result = await state.bridge.apiPost("dashboard/config-migration", { token: state.migrationToken });
      state.migrationToken = null;
      setText("migration-status", result.message);
      setText("migration-fields", `已导入 ${result.count} 项配置。`);
    } catch (err) {
      state.migrationToken = null;
      setText("migration-status", `导入失败：${err.message || err}；请重新预览。`);
    }
  }

  async function init() {
    try {
      state.bridge = await getBridge();
      const context = await state.bridge.ready();
      $("plugin-link").href = `#/extension/plugins/${encodeURIComponent(context.pluginName)}`;
      $("plugin-refresh-link").href = `#/plugin-view/${encodeURIComponent(context.pluginName)}/dashboard`;
    } catch (err) {
      setText("page-subtitle", err.message || String(err));
      return;
    }

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.dataset.view));
    });
    $("btn-migration-preview").addEventListener("click", previewMigration);
    $("btn-migration-apply").addEventListener("click", applyMigration);
    $("btn-refresh").addEventListener("click", () => {
      refreshOverview();
      refreshSeries();
    });
    $("btn-learning-refresh").addEventListener("click", loadLearning);
    $("btn-learning-search").addEventListener("click", loadLearning);
    $("learning-query").addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadLearning();
    });
    $("learning-limit").addEventListener("change", loadLearning);
    $("btn-images-refresh").addEventListener("click", loadImages);
    const searchImages = () => { state.imagesOffset = 0; loadImages(); };
    $("btn-images-search").addEventListener("click", searchImages);
    $("images-query").addEventListener("keydown", e => { if (e.key === "Enter") searchImages(); });
    $("images-prev").addEventListener("click", () => { state.imagesOffset = Math.max(0, state.imagesOffset - 30); loadImages(); });
    $("images-next").addEventListener("click", () => { state.imagesOffset += 30; loadImages(); });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      if (state.view === "dashboard") {
        refreshOverview();
        refreshSeries();
      } else if (state.view === "learning") {
        loadLearning();
      } else if (state.view === "images") {
        loadImages();
      }
    });
    window.addEventListener("resize", redrawCharts);

    switchView("dashboard");
  }

  init();
})();

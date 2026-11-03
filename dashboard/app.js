/**
 * Dashboard Frontend Controller
 * 
 * Handles:
 * - Real-time WebSocket event subscription (/ws/telemetry)
 * - Dynamic SVG DAG generation and real-time status transitions
 * - Interactive build and benchmark controls
 * - Live KPI metrics strip and task execution inspector
 */

// ── STATE ──
let ws = null;
let graphData = { nodes: [], edges: [], targets: [] };
let selectedNode = null;
let buildTimer = null;
let buildStartTime = 0;
let isBuilding = false;
const currentTaskStatuses = {};
let statusFetchTimeout = null;

const STATUS_PRECEDENCE = {
  PENDING: 0,
  READY: 1,
  RUNNING: 2,
  COMPLETED: 3,
  SKIPPED: 3,
  FAILED: 3,
  BLOCKED: 3,
};

function requestStatusUpdate() {
  if (statusFetchTimeout) return;
  statusFetchTimeout = setTimeout(() => {
    statusFetchTimeout = null;
    fetchStatus();
  }, 100);
}

// ── DOM ELEMENTS ──
const connectionPill = document.getElementById("connectionPill");
const buildIdBadge = document.getElementById("buildIdBadge");
const targetSelect = document.getElementById("targetSelect");
const workersSelect = document.getElementById("workersSelect");
const cleanBuildCheckbox = document.getElementById("cleanBuildCheckbox");
const runBuildBtn = document.getElementById("runBuildBtn");
const runBenchmarkBtn = document.getElementById("runBenchmarkBtn");
const refreshBtn = document.getElementById("refreshBtn");

const metricDuration = document.getElementById("metricDuration");
const metricStatus = document.getElementById("metricStatus");
const metricWorkers = document.getElementById("metricWorkers");
const metricPeak = document.getElementById("metricPeak");
const metricCompleted = document.getElementById("metricCompleted");
const metricSkipped = document.getElementById("metricSkipped");
const metricFailedBlocked = document.getElementById("metricFailedBlocked");
const metricFailureSub = document.getElementById("metricFailureSub");
const metricSpeedup = document.getElementById("metricSpeedup");
const metricEfficiency = document.getElementById("metricEfficiency");
const progressBar = document.getElementById("progressBar");

const dagSvg = document.getElementById("dagSvg");
const eventStream = document.getElementById("eventStream");
const eventCountLabel = document.getElementById("eventCountLabel");
const tasksTableBody = document.getElementById("tasksTableBody");
const benchTableBody = document.getElementById("benchTableBody");
const benchmarkSummary = document.getElementById("benchmarkSummary");
const nodeInspector = document.getElementById("nodeInspector");
const clearLogBtn = document.getElementById("clearLogBtn");

let eventCount = 0;

// ── INITIALIZATION ──
document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  setupEventListeners();
  initWebSocket();
  loadGraph();
  loadComparisons();
});

// ── WEBSOCKET CLIENT ──
function initWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/telemetry`;

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    connectionPill.className = "status-pill connected";
    connectionPill.textContent = "● Connected (Live)";
    // Refresh state on connect/reconnect
    loadGraph();
    fetchStatus();
    loadComparisons();
  };

  ws.onclose = () => {
    connectionPill.className = "status-pill disconnected";
    connectionPill.textContent = "● Reconnecting...";
    setTimeout(initWebSocket, 2000);
  };

  ws.onerror = () => {
    ws.close();
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "EVENT") {
        handleTelemetryEvent(msg.data);
      } else if (msg.type === "INIT") {
        updateMetrics(msg.data);
      }
    } catch (e) {
      console.error("Failed to parse WS message:", e);
    }
  };
}

// ── EVENT HANDLING ──
function handleTelemetryEvent(evt) {
  eventCount++;
  eventCountLabel.textContent = `${eventCount} events`;

  // Append to stream
  addEventToStream(evt);

  // Update node in DAG and task table
  if (evt.task_id) {
    const prevStatus = currentTaskStatuses[evt.task_id];
    const prevRank = STATUS_PRECEDENCE[prevStatus] || 0;
    const newRank = STATUS_PRECEDENCE[evt.status] || 0;
    const isDemotion = prevRank >= 3 && newRank < 3;

    if (!isDemotion) {
      updateNodeStatus(evt.task_id, evt.status, evt.duration, evt.worker_id);
      updateTaskTableRow(evt.task_id, evt.status, evt.duration, evt.worker_id);
      if (selectedNode && selectedNode.id === evt.task_id) {
        selectedNode.status = evt.status;
        if (evt.duration) selectedNode.duration = evt.duration;
        if (evt.worker_id) selectedNode.worker_id = evt.worker_id;
        if (evt.stdout) selectedNode.stdout = evt.stdout;
        if (evt.stderr) selectedNode.stderr = evt.stderr;
        renderInspector(selectedNode);
      }
    }
  }

  // Handle Build Lifecycle Transitions
  if (evt.event_type === "BUILD_STARTED") {
    isBuilding = true;
    buildIdBadge.textContent = `Session: ${evt.build_id.slice(-8)}`;
    setBuildingUI(true);
    startDurationTimer();

    // Reset all task statuses to PENDING
    for (const key of Object.keys(currentTaskStatuses)) {
      delete currentTaskStatuses[key];
    }
    if (graphData && graphData.nodes) {
      graphData.nodes.forEach(n => {
        n.status = "PENDING";
        n.duration = 0;
        n.worker_id = null;
        updateNodeStatus(n.id, "PENDING", null, null, true);
        updateTaskTableRow(n.id, "PENDING", null, null);
      });
    }
  } else if (evt.event_type === "BUILD_COMPLETED" || evt.event_type === "BUILD_FAILED") {
    isBuilding = false;
    stopDurationTimer();
    setBuildingUI(false);
    fetchStatus();
    loadComparisons();
  }

  // Debounced status polling to avoid network contention during bursts
  requestStatusUpdate();
}

function addEventToStream(evt) {
  const placeholder = eventStream.querySelector(".placeholder");
  if (placeholder) placeholder.remove();

  const row = document.createElement("div");
  row.className = "event-row";

  const timeStr = evt.iso_time ? evt.iso_time.split("T")[1].slice(0, 12) : new Date().toLocaleTimeString();
  const taskLabel = evt.task_name ? evt.task_name.split("/").pop() : "";

  row.innerHTML = `
    <span class="event-time">${timeStr}</span>
    <span class="event-type-badge badge-${evt.event_type}">${evt.event_type}</span>
    <span class="event-details">
      ${taskLabel ? `<strong>${taskLabel}</strong>: ` : ""}
      ${evt.message || ""}
    </span>
  `;

  eventStream.appendChild(row);
  eventStream.scrollTop = eventStream.scrollHeight;
}

// ── REST API CALLS ──
async function loadGraph() {
  const target = targetSelect.value;
  try {
    const res = await fetch(`/api/graph?target=${encodeURIComponent(target)}`);
    if (!res.ok) throw new Error("Failed to load graph");
    const freshData = await res.json();

    // If a build is actively running and we already have rendered nodes,
    // do not rebuild SVG from scratch, which would wipe in-flight visual state
    // and recreate nodes with stale status snapshots from /api/graph.
    if (isBuilding && dagSvg.querySelector(".dag-node")) {
      graphData.targets = freshData.targets;
      return;
    }

    graphData = freshData;

    // Preserve higher-priority local live statuses over stale snapshot data
    if (graphData.nodes) {
      graphData.nodes.forEach(n => {
        if (currentTaskStatuses[n.id]) {
          const localRank = STATUS_PRECEDENCE[currentTaskStatuses[n.id]] || 0;
          const snapRank = STATUS_PRECEDENCE[n.status] || 0;
          if (localRank >= snapRank) {
            n.status = currentTaskStatuses[n.id];
          }
        }
      });
    }

    renderDag(graphData);
    populateTaskTable(graphData.nodes);
    fetchStatus();
  } catch (e) {
    console.error("Error loading graph:", e);
  }
}

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    if (res.ok) {
      const data = await res.json();
      updateMetrics(data);
    }
  } catch (e) {
    console.error("Error fetching status:", e);
  }
}

async function loadComparisons() {
  try {
    const res = await fetch("/api/comparison");
    if (!res.ok) return;
    const data = await res.json();

    if (data.latest) {
      metricSpeedup.textContent = `${data.latest.speedup}x`;
      metricEfficiency.textContent = `Efficiency: ${data.latest.efficiency}% (${data.latest.worker_count}w)`;

      benchmarkSummary.innerHTML = `
        <div style="font-size: 14px; font-weight: 700; margin-bottom: 6px; color: var(--color-blue)">
          Latest Benchmark (${data.latest.target})
        </div>
        <div>Sequential (1 Worker): <strong>${data.latest.sequential_duration.toFixed(3)}s</strong></div>
        <div>Parallel (${data.latest.worker_count} Workers): <strong>${data.latest.parallel_duration.toFixed(3)}s</strong></div>
        <div>Speedup: <strong class="text-green">${data.latest.speedup}x</strong></div>
        <div>Efficiency: <strong class="text-cyan">${data.latest.efficiency}%</strong></div>
        <div>Time Saved: <strong>${data.latest.time_saved.toFixed(3)}s (${data.latest.pct_reduction}%)</strong></div>
      `;
    }

    if (data.history && data.history.length > 0) {
      benchTableBody.innerHTML = data.history.map(c => `
        <tr>
          <td>${new Date(c.timestamp * 1000).toLocaleTimeString()}</td>
          <td>${c.target}</td>
          <td>${c.sequential_duration.toFixed(3)}s</td>
          <td>${c.parallel_duration.toFixed(3)}s</td>
          <td><strong class="text-green">${c.speedup}x</strong></td>
          <td>${c.efficiency}%</td>
        </tr>
      `).join("");
    }
  } catch (e) {
    console.error("Error loading comparisons:", e);
  }
}

// ── UI UPDATES ──
function updateMetrics(data) {
  if (!isBuilding) {
    metricDuration.textContent = `${(data.wall_clock_duration || 0).toFixed(3)} s`;
    metricStatus.textContent = data.status || "IDLE";
  }

  metricWorkers.textContent = `${data.active_workers || 0} / ${data.max_workers || 4}`;
  metricPeak.textContent = `Peak: ${data.peak_concurrency || 0} active`;

  const tasks = data.tasks || {};
  const total = tasks.total || graphData.nodes.length || 0;
  const completed = tasks.completed || 0;
  const skipped = tasks.skipped || 0;
  const failed = tasks.failed || 0;
  const blocked = tasks.blocked || 0;

  metricCompleted.textContent = `${completed} / ${total}`;
  metricSkipped.textContent = `${skipped}`;
  metricFailedBlocked.textContent = `${failed} / ${blocked}`;

  if (failed > 0 || blocked > 0) {
    metricFailureSub.textContent = `${failed} failed, ${blocked} blocked`;
    metricFailureSub.style.color = "var(--color-red)";
  } else {
    metricFailureSub.textContent = "No errors";
    metricFailureSub.style.color = "var(--text-muted)";
  }

  const pct = total > 0 ? Math.round(((completed + skipped) / total) * 100) : 0;
  progressBar.style.width = `${pct}%`;
}

function startDurationTimer() {
  buildStartTime = Date.now();
  if (buildTimer) clearInterval(buildTimer);
  metricStatus.textContent = "BUILDING...";
  metricStatus.style.color = "var(--color-blue)";
  buildTimer = setInterval(() => {
    const elapsed = (Date.now() - buildStartTime) / 1000;
    metricDuration.textContent = `${elapsed.toFixed(3)} s`;
  }, 50);
}

function stopDurationTimer() {
  if (buildTimer) {
    clearInterval(buildTimer);
    buildTimer = null;
  }
  metricStatus.style.color = "var(--text-muted)";
}

function setBuildingUI(building) {
  runBuildBtn.disabled = building;
  runBenchmarkBtn.disabled = building;
  if (building) {
    runBuildBtn.innerHTML = `<span class="btn-icon">⏳</span> Building...`;
  } else {
    runBuildBtn.innerHTML = `<span class="btn-icon">▶</span> Run Build`;
    runBenchmarkBtn.innerHTML = `<span class="btn-icon">📊</span> Run Benchmark (1w vs 4w)`;
  }
}

// ── SVG DAG RENDERER ──
function renderDag(graph) {
  dagSvg.innerHTML = "";
  if (!graph.nodes || graph.nodes.length === 0) return;

  // 1. Calculate topological levels
  const inDegree = {};
  const adj = {};
  const revAdj = {};
  graph.nodes.forEach(n => {
    inDegree[n.id] = 0;
    adj[n.id] = [];
    revAdj[n.id] = [];
  });

  graph.edges.forEach(e => {
    if (adj[e.from]) adj[e.from].push(e.to);
    if (revAdj[e.to]) {
      revAdj[e.to].push(e.from);
      inDegree[e.to]++;
    }
  });

  const levels = {};
  const queue = graph.nodes.filter(n => inDegree[n.id] === 0).map(n => n.id);
  queue.forEach(id => { levels[id] = 0; });

  while (queue.length > 0) {
    const u = queue.shift();
    const curLevel = levels[u];
    (adj[u] || []).forEach(v => {
      levels[v] = Math.max(levels[v] || 0, curLevel + 1);
      inDegree[v]--;
      if (inDegree[v] === 0) queue.push(v);
    });
  }

  // Handle any nodes not reached by topological sort
  graph.nodes.forEach(n => {
    if (levels[n.id] === undefined) levels[n.id] = 0;
  });

  // Group nodes by level
  const levelGroups = {};
  graph.nodes.forEach(n => {
    const lvl = levels[n.id];
    if (!levelGroups[lvl]) levelGroups[lvl] = [];
    levelGroups[lvl].push(n);
  });

  // Add arrow markers to SVG
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  defs.innerHTML = `
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 9 5 L 0 9 z" fill="#58a6ff" />
    </marker>
    <marker id="arrow-implicit" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 9 5 L 0 9 z" fill="#8b949e" />
    </marker>
  `;
  dagSvg.appendChild(defs);

  const NODE_WIDTH = 210;
  const NODE_HEIGHT = 56;
  const LEVEL_GAP_X = 140;
  const NODE_GAP_Y = 24;

  const positions = {};
  let maxX = 0;
  let maxY = 0;

  // Compute maximum number of nodes in any single level for vertical centering
  const maxNodesInAnyLevel = Math.max(...Object.values(levelGroups).map(grp => grp.length), 1);
  const totalMaxHeight = maxNodesInAnyLevel * (NODE_HEIGHT + NODE_GAP_Y) - NODE_GAP_Y;

  Object.keys(levelGroups).sort((a,b) => a - b).forEach(lvlStr => {
    const lvl = parseInt(lvlStr);
    const nodesInLevel = levelGroups[lvl];
    const x = 40 + lvl * (NODE_WIDTH + LEVEL_GAP_X);
    if (x + NODE_WIDTH > maxX) maxX = x + NODE_WIDTH;

    const colHeight = nodesInLevel.length * (NODE_HEIGHT + NODE_GAP_Y) - NODE_GAP_Y;
    const yOffset = Math.max(0, (totalMaxHeight - colHeight) / 2);

    nodesInLevel.forEach((n, idx) => {
      const y = 40 + yOffset + idx * (NODE_HEIGHT + NODE_GAP_Y);
      positions[n.id] = { x, y };
      if (y + NODE_HEIGHT > maxY) maxY = y + NODE_HEIGHT;
    });
  });

  dagSvg.setAttribute("width", `${maxX + 60}px`);
  dagSvg.setAttribute("height", `${maxY + 60}px`);

  // Draw Edges
  graph.edges.forEach(e => {
    const p1 = positions[e.from];
    const p2 = positions[e.to];
    if (!p1 || !p2) return;

    const startX = p1.x + NODE_WIDTH;
    const startY = p1.y + NODE_HEIGHT / 2;
    const endX = p2.x;
    const endY = p2.y + NODE_HEIGHT / 2;
    const midX = (startX + endX) / 2;

    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute(
      "d",
      `M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`
    );
    path.setAttribute("class", `dag-edge ${e.type}`);
    path.setAttribute("id", `edge-${e.from}-${e.to}`);
    path.setAttribute("marker-end", e.type === "implicit" ? "url(#arrow-implicit)" : "url(#arrow)");
    dagSvg.appendChild(path);
  });

  // Draw Nodes
  graph.nodes.forEach(n => {
    const pos = positions[n.id];
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("class", `dag-node ${n.status}`);
    g.setAttribute("id", `node-${n.id}`);
    g.setAttribute("transform", `translate(${pos.x}, ${pos.y})`);

    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("width", NODE_WIDTH);
    rect.setAttribute("height", NODE_HEIGHT);

    // Extract clean display basename
    const rawLabel = n.label || n.id;
    const basename = rawLabel.split(/[/\\]/).pop();

    let targetGroup = "task";
    if (n.id.includes("mylib")) targetGroup = "mylib";
    else if (n.id.includes("test_runner")) targetGroup = "test_runner";
    else if (n.id.includes("demo")) targetGroup = "demo";

    const title = document.createElementNS("http://www.w3.org/2000/svg", "text");
    title.setAttribute("class", "node-title");
    title.setAttribute("x", "12");
    title.setAttribute("y", "24");
    title.textContent = truncate(basename, 22);

    const subtitle = document.createElementNS("http://www.w3.org/2000/svg", "text");
    subtitle.setAttribute("class", "node-subtitle");
    subtitle.setAttribute("x", "12");
    subtitle.setAttribute("y", "44");
    subtitle.setAttribute("id", `sub-${n.id}`);
    const durStr = n.duration ? `${n.duration.toFixed(3)}s` : n.status;
    subtitle.textContent = `${targetGroup} | ${durStr}`;

    g.appendChild(rect);
    g.appendChild(title);
    g.appendChild(subtitle);

    g.addEventListener("click", () => {
      selectNode(n);
    });

    dagSvg.appendChild(g);
  });
}

function updateNodeStatus(taskId, status, duration, workerId, force = false) {
  const prevStatus = currentTaskStatuses[taskId];
  const prevRank = STATUS_PRECEDENCE[prevStatus] || 0;
  const newRank = STATUS_PRECEDENCE[status] || 0;
  if (!force && prevRank >= 3 && newRank < 3) {
    return;
  }
  currentTaskStatuses[taskId] = status;

  if (graphData && graphData.nodes) {
    const nodeObj = graphData.nodes.find(n => n.id === taskId);
    if (nodeObj) {
      nodeObj.status = status;
      if (duration) nodeObj.duration = duration;
      if (workerId) nodeObj.worker_id = workerId;
    }
  }

  const g = document.getElementById(`node-${taskId}`);
  if (g) {
    g.setAttribute("class", `dag-node ${status}`);
    g.className.baseVal = `dag-node ${status}`;
    const sub = document.getElementById(`sub-${taskId}`);
    if (sub) {
      const durText = duration ? `${duration.toFixed(3)}s` : status;
      const workerText = workerId ? ` (w${workerId})` : "";
      sub.textContent = `${durText}${workerText}`;
    }
  }
}

function selectNode(node) {
  selectedNode = node;
  // Switch to inspector tab
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
  const inspectorTabBtn = document.querySelector('[data-tab="detailsTab"]');
  const inspectorContent = document.getElementById("detailsTab");
  if (inspectorTabBtn && inspectorContent) {
    inspectorTabBtn.classList.add("active");
    inspectorContent.classList.add("active");
  }
  renderInspector(node);
}

function renderInspector(node) {
  nodeInspector.innerHTML = `
    <div class="inspect-header">
      <div class="inspect-title">${node.label || node.id}</div>
      <div style="font-size: 11px; color: var(--text-secondary); margin-top: 4px;">
        Rule: <strong>${node.rule}</strong> | Status: <strong class="text-cyan">${node.status}</strong>
        ${node.worker_id ? ` | Worker: ${node.worker_id}` : ""}
        ${node.duration ? ` | Duration: ${node.duration.toFixed(3)}s` : ""}
      </div>
    </div>
    
    <div class="inspect-field">
      <div class="inspect-label">Outputs:</div>
      <div class="inspect-box">${(node.outputs || []).join("\n") || "None"}</div>
    </div>

    <div class="inspect-field">
      <div class="inspect-label">Inputs:</div>
      <div class="inspect-box">${(node.inputs || []).join("\n") || "None"}</div>
    </div>

    <div class="inspect-field">
      <div class="inspect-label">Command:</div>
      <div class="inspect-box">${node.command || "No command"}</div>
    </div>

    ${node.stdout ? `
      <div class="inspect-field">
        <div class="inspect-label">Standard Output:</div>
        <div class="inspect-box">${escapeHtml(node.stdout)}</div>
      </div>
    ` : ""}

    ${node.stderr || node.error_message ? `
      <div class="inspect-field">
        <div class="inspect-label" style="color: var(--color-red);">Standard Error / Error:</div>
        <div class="inspect-box" style="border-color: rgba(248,81,73,0.3); color: var(--color-red);">${escapeHtml(node.stderr || node.error_message)}</div>
      </div>
    ` : ""}
  `;
}

// ── TASK TABLE ──
function populateTaskTable(nodes) {
  if (!nodes || nodes.length === 0) {
    tasksTableBody.innerHTML = `<tr><td colspan="4" class="text-center">No tasks</td></tr>`;
    return;
  }

  tasksTableBody.innerHTML = nodes.map(n => `
    <tr id="task-row-${n.id}">
      <td>${truncate(n.label || n.id, 28)}</td>
      <td><span class="badge-status status-${n.status}" id="row-status-${n.id}">${n.status}</span></td>
      <td id="row-worker-${n.id}">${n.worker_id ? `Worker ${n.worker_id}` : "—"}</td>
      <td id="row-duration-${n.id}">${n.duration ? `${n.duration.toFixed(3)}s` : "—"}</td>
    </tr>
  `).join("");
}

function updateTaskTableRow(taskId, status, duration, workerId) {
  const rowStatus = document.getElementById(`row-status-${taskId}`);
  const rowWorker = document.getElementById(`row-worker-${taskId}`);
  const rowDuration = document.getElementById(`row-duration-${taskId}`);

  if (rowStatus) {
    rowStatus.className = `badge-status status-${status}`;
    rowStatus.textContent = status;
  }
  if (rowWorker && workerId) {
    rowWorker.textContent = `Worker ${workerId}`;
  }
  if (rowDuration && duration) {
    rowDuration.textContent = `${duration.toFixed(3)}s`;
  }
}

// ── CONTROLS & EVENT LISTENERS ──
function setupEventListeners() {
  targetSelect.addEventListener("change", () => {
    loadGraph();
  });

  refreshBtn.addEventListener("click", () => {
    loadGraph();
    fetchStatus();
    loadComparisons();
  });

  clearLogBtn.addEventListener("click", () => {
    eventStream.innerHTML = `<div class="event-row placeholder">Event stream cleared.</div>`;
    eventCount = 0;
    eventCountLabel.textContent = `0 events`;
  });

  runBuildBtn.addEventListener("click", async () => {
    if (isBuilding) return;
    const target = targetSelect.value;
    const max_workers = parseInt(workersSelect.value);
    const clean = cleanBuildCheckbox.checked;

    try {
      const res = await fetch("/api/build/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, max_workers, clean }),
      });
      if (!res.ok) {
        const err = await res.json();
        alert(err.detail || "Build request failed");
      }
    } catch (e) {
      console.error("Build failed to launch:", e);
    }
  });

  runBenchmarkBtn.addEventListener("click", async () => {
    if (isBuilding) return;
    const target = targetSelect.value;
    runBenchmarkBtn.disabled = true;
    runBenchmarkBtn.innerHTML = `<span class="btn-icon">⏳</span> Benchmarking...`;

    try {
      const res = await fetch("/api/benchmark/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, parallel_workers: 4 }),
      });
      if (res.ok) {
        await loadComparisons();
      } else {
        const err = await res.json();
        alert(err.detail || "Benchmark failed");
      }
    } catch (e) {
      console.error("Benchmark error:", e);
    } finally {
      runBenchmarkBtn.disabled = false;
      runBenchmarkBtn.innerHTML = `<span class="btn-icon">📊</span> Run Benchmark (1w vs 4w)`;
    }
  });
}

function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  const tabContents = document.querySelectorAll(".tab-content");

  tabButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const targetTab = btn.getAttribute("data-tab");
      tabButtons.forEach(b => b.classList.remove("active"));
      tabContents.forEach(c => c.classList.remove("active"));

      btn.classList.add("active");
      const content = document.getElementById(targetTab);
      if (content) content.classList.add("active");
    });
  });
}

// ── UTILITIES ──
function truncate(str, maxLen) {
  if (!str) return "";
  return str.length > maxLen ? str.slice(0, maxLen - 1) + "…" : str;
}

function escapeHtml(text) {
  if (!text) return "";
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

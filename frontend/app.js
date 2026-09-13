const API_BASE = window.DAG_MILE_API || "http://127.0.0.1:8000";

const state = {
  sessionId: null,
  pendingFiles: [],      // File objects, before upload
  uploadedFiles: [],     // [{filename, rows, cols, columns}]
  relationships: [],
  resolutions: {},       // key -> decision label, for UI state
  currentPreprocessFile: null,
  suggestions: null,
};

// ---------------------------------------------------------------- helpers
function $(sel) { return document.querySelector(sel); }
function $all(sel) { return Array.from(document.querySelectorAll(sel)); }

async function api(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, options);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

function showStep(name) {
  $all(".panel").forEach(p => p.classList.add("hidden"));
  $(`#panel-${name}`).classList.remove("hidden");
  $all(".step").forEach(s => {
    s.classList.toggle("active", s.dataset.step === name);
  });
}

$all("[data-back]").forEach(btn => {
  btn.addEventListener("click", () => showStep(btn.dataset.back));
});

// ---------------------------------------------------------------- STEP 1: upload
const dropzone = $("#dropzone");
const fileInput = $("#fileInput");

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", e => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", e => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  addPendingFiles(e.dataTransfer.files);
});
fileInput.addEventListener("change", () => addPendingFiles(fileInput.files));

function addPendingFiles(fileList) {
  const allowed = [
    ".csv",
    ".tsv",
    ".xlsx",
    ".xls",
    ".xlsm",
    ".xlsb",
    ".json",
    ".ods"
  ];

  for (const f of fileList) {
    const name = f.name.toLowerCase();

    // Ignore Excel temporary/lock files
    if (name.startsWith("~$")) {
      continue;
    }

    if (allowed.some(ext => name.endsWith(ext))) {
      state.pendingFiles.push(f);
    }
  }

  renderPendingFiles();
}

function renderPendingFiles() {
  const ul = $("#pendingFiles");
  ul.innerHTML = "";
  state.pendingFiles.forEach((f, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${f.name}</span>`;
    const rm = document.createElement("button");
    rm.textContent = "✕";
    rm.addEventListener("click", () => {
      state.pendingFiles.splice(i, 1);
      renderPendingFiles();
    });
    li.appendChild(rm);
    ul.appendChild(li);
  });
  $("#analyzeBtn").disabled = state.pendingFiles.length === 0;
}

$("#analyzeBtn").addEventListener("click", async () => {
  const btn = $("#analyzeBtn");
  btn.disabled = true;
  btn.textContent = "Uploading…";
  try {
    const form = new FormData();
    if (state.sessionId) form.append("session_id", state.sessionId);
    state.pendingFiles.forEach(f => form.append("files", f));

    const uploadRes = await api("/api/upload", { method: "POST", body: form });
    state.sessionId = uploadRes.session_id;
    state.uploadedFiles = state.uploadedFiles.concat(uploadRes.files);
    state.pendingFiles = [];
    renderPendingFiles();

    if (uploadRes.errors && uploadRes.errors.length) {
      alert("Some files could not be read:\n" + uploadRes.errors.map(e => `${e.filename}: ${e.error}`).join("\n"));
    }

    await runAnalysis();
    showStep("analysis");
  } catch (err) {
    alert("Upload failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Analyze →";
  }
});

// ---------------------------------------------------------------- STEP 2: analysis
async function runAnalysis() {
  const data = await api(`/api/analyze/${state.sessionId}`);
  state.relationships = data.relationships;
  state.profiles = data.profiles;

  $("#fileCount").textContent = Object.keys(data.profiles).length;
  const tbody = $("#fileTable tbody");
  tbody.innerHTML = "";
  for (const [fname, p] of Object.entries(data.profiles)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="mono-cell">${fname}</td>
      <td class="mono-cell">${p.n_rows.toLocaleString()} × ${p.n_cols}</td>
      <td>${p.missing_cells_pct}%</td>
      <td>${p.duplicate_rows} (${p.duplicate_pct}%)</td>
    `;
    tbody.appendChild(tr);
  }
}

$("#toSchemaBtn").addEventListener("click", () => {
  renderConflicts();
  populateMergeSelectors();
  showStep("schema");
});

// ---------------------------------------------------------------- STEP 3: schema matching
function renderConflicts() {
  const container = $("#conflictList");
  const empty = $("#noConflicts");
  container.innerHTML = "";

  if (!state.relationships.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const summary = document.createElement("div");
  summary.className = "schema-summary";
  summary.textContent = `${state.relationships.length} meaningful schema relationship${state.relationships.length === 1 ? "" : "s"} found`;
  container.appendChild(summary);

  state.relationships.forEach((r, idx) => {
    const key = `${r.file_a}:${r.column_a}::${r.file_b}:${r.column_b}`;
    const card = document.createElement("div");
    const cls = r.relationship === "conflict" ? "" : r.relationship === "join_key" ? "join-key" : "same-feature";
    card.className = `conflict-card ${cls}`;

    const headlineText = r.relationship === "conflict" ? "⚠ POSSIBLE FEATURE CONFLICT"
      : r.relationship === "join_key" ? "🔗 POSSIBLE JOIN KEY"
      : "≈ POSSIBLY THE SAME FEATURE";

    card.innerHTML = `
      <div class="headline">${headlineText}</div>
      <div class="cols">${r.file_a} → ${r.column_a}  /  ${r.file_b} → ${r.column_b}${r.files_count > 2 ? ` · present in ${r.files_count} files` : ""}</div>
      <div class="note">${r.note}</div>
      <div class="confidence">Recommendation: <strong>${r.recommendation}</strong> · Confidence: ${Math.round(r.confidence * 100)}%</div>
      <div class="row-actions" data-key="${key}"></div>
    `;

    const actions = card.querySelector(".row-actions");
    if (r.relationship === "conflict") {
      addActionButton(actions, key, "keep_separate", "Keep Separate", r);
      addActionButton(actions, key, "rename_a", `Rename '${r.column_a}' in ${r.file_a}`, r);
      addActionButton(actions, key, "rename_b", `Rename '${r.column_b}' in ${r.file_b}`, r);
    } else {
      addActionButton(actions, key, "keep_separate", "Acknowledge", r);
    }
    container.appendChild(card);
  });
}

function addActionButton(container, key, decision, label, r) {
  const btn = document.createElement("button");
  btn.textContent = label;
  btn.addEventListener("click", async () => {
    let newName = null;
    if (decision.startsWith("rename")) {
      const current = decision === "rename_a" ? r.column_a : r.column_b;
      newName = prompt(`New name for '${current}':`, `${current}_${decision === "rename_a" ? r.file_a.split(".")[0] : r.file_b.split(".")[0]}`);
      if (!newName) return;
    }
    try {
      await api(`/api/resolve-conflict/${state.sessionId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          file_a: r.file_a, column_a: r.column_a,
          file_b: r.file_b, column_b: r.column_b,
          decision, new_name: newName,
        }),
      });
      container.querySelectorAll("button").forEach(b => b.classList.remove("chosen"));
      btn.classList.add("chosen");
      if (newName) refreshFileColumnsAfterRename();
    } catch (err) {
      alert("Could not apply: " + err.message);
    }
  });
  container.appendChild(btn);
}

async function refreshFileColumnsAfterRename() {
  await runAnalysis();
}

function populateMergeSelectors() {
  const fileNames = state.uploadedFiles.map(f => f.filename);
  fillSelect($("#mergeFileA"), fileNames);
  fillSelect($("#mergeFileB"), fileNames.length > 1 ? [fileNames[1], ...fileNames.filter((_, i) => i !== 1)] : fileNames);
  updateMergeColumnOptions();
}

function fillSelect(select, options) {
  select.innerHTML = "";
  options.forEach(o => {
    const opt = document.createElement("option");
    opt.value = o; opt.textContent = o;
    select.appendChild(opt);
  });
}

function updateMergeColumnOptions() {
  const fa = state.uploadedFiles.find(f => f.filename === $("#mergeFileA").value);
  const fb = state.uploadedFiles.find(f => f.filename === $("#mergeFileB").value);
  fillSelect($("#mergeColA"), fa ? fa.columns : []);
  fillSelect($("#mergeColB"), fb ? fb.columns : []);
}
$("#mergeFileA").addEventListener("change", updateMergeColumnOptions);
$("#mergeFileB").addEventListener("change", updateMergeColumnOptions);
$("#mergeOp").addEventListener("change", () => {
  $("#joinKeysRow").classList.toggle("hidden", $("#mergeOp").value !== "join");
});

$("#mergeBtn").addEventListener("click", async () => {
  const op = $("#mergeOp").value;
  const fileA = $("#mergeFileA").value;
  const fileB = $("#mergeFileB").value;
  const newName = $("#mergeName").value.trim() || `merged_${Date.now()}.csv`;
  if (fileA === fileB) { alert("Choose two different files."); return; }

  const body = { operation: op, file_a: fileA, file_b: fileB, new_name: newName };
  if (op === "join") {
    body.column_a = $("#mergeColA").value;
    body.column_b = $("#mergeColB").value;
    body.how = $("#mergeHow").value;
  }

  const status = $("#mergeStatus");
  status.textContent = "Merging…";
  try {
    const res = await api(`/api/merge/${state.sessionId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.uploadedFiles.push(res.new_file);
    status.textContent = `✓ Created ${res.new_file.filename} (${res.new_file.rows} rows × ${res.new_file.cols} cols)`;
    populateMergeSelectors();
    await runAnalysis();
  } catch (err) {
    status.textContent = "";
    alert("Merge failed: " + err.message);
  }
});

$("#toPreprocessBtn").addEventListener("click", async () => {
  fillSelect($("#preprocessFileSelect"), state.uploadedFiles.map(f => f.filename));
  state.currentPreprocessFile = $("#preprocessFileSelect").value;
  await loadSuggestions();
  showStep("preprocess");
});

// ---------------------------------------------------------------- STEP 4: preprocessing
$("#preprocessFileSelect").addEventListener("change", async () => {
  state.currentPreprocessFile = $("#preprocessFileSelect").value;
  await loadSuggestions();
});

async function loadSuggestions() {
  if (!state.currentPreprocessFile) return;
  const data = await api(`/api/preprocess-suggestions/${state.sessionId}/${encodeURIComponent(state.currentPreprocessFile)}`);
  state.suggestions = data;
  renderQualitySummary(data);
  renderSuggestTable(data);
  $("#dupCount").textContent = data.duplicate_rows;
  $("#dropDupCheckbox").checked = data.duplicate_rows > 0;
}

function renderQualitySummary(data) {
  const box = $("#qualitySummary");
  box.innerHTML = `
    <div class="metric"><span class="value">${data.n_rows.toLocaleString()}</span><span class="label">ROWS</span></div>
    <div class="metric"><span class="value">${data.duplicate_pct}%</span><span class="label">DUPLICATE ROWS</span></div>
    <div class="metric"><span class="value">${data.outlier_rows_estimate}</span><span class="label">POTENTIAL OUTLIERS</span></div>
  `;
}

const MISSING_OPTIONS = {
  numeric: ["none", "median", "mean", "constant", "drop_rows"],
  categorical: ["none", "most_frequent", "constant", "drop_rows"],
  boolean: ["none", "most_frequent", "constant", "drop_rows"],
  identifier: ["none", "drop_rows"],
  text: ["none", "drop_rows"],
  datetime: ["none", "drop_rows"],
};
const ENCODE_OPTIONS = ["none", "onehot", "label"];
const SCALE_OPTIONS = ["none", "standard", "minmax"];
const OUTLIER_OPTIONS = ["none", "keep", "winsorize", "remove"];

function renderSuggestTable(data) {
  const tbody = $("#suggestTable tbody");
  tbody.innerHTML = "";
  data.columns.forEach(col => {
    const tr = document.createElement("tr");
    tr.dataset.column = col.column;
    tr.dataset.type = col.feature_type;

    const missingCell = col.missing_count > 0
      ? `${col.missing_count} (${col.missing_pct}%)` : "—";
    const outlierCell = col.outlier_count !== undefined
      ? (col.outlier_count > 0 ? `${col.outlier_count} (${col.outlier_pct}%)` : "—")
      : "—";

    tr.innerHTML = `
      <td class="mono-cell">${col.column}</td>
      <td class="mono-cell">${col.feature_type}</td>
      <td>${missingCell}</td>
      <td>${selectHtml("missing", MISSING_OPTIONS[col.feature_type] || ["none"], col.missing_strategy || "none")}</td>
      <td>${outlierCell}</td>
      <td>${col.feature_type === "numeric" ? selectHtml("outlier", OUTLIER_OPTIONS, col.outlier_action || "none") : "—"}</td>
      <td>${col.feature_type === "categorical" || col.feature_type === "boolean" ? selectHtml("encode", ENCODE_OPTIONS, col.encode || "none") : "—"}</td>
      <td>${col.feature_type === "numeric" ? selectHtml("scale", SCALE_OPTIONS, col.scale || "none") : "—"}</td>
    `;
    tbody.appendChild(tr);
  });
}

function selectHtml(name, options, selected) {
  const opts = options.map(o => `<option value="${o}" ${o === selected ? "selected" : ""}>${o}</option>`).join("");
  return `<select data-field="${name}">${opts}</select>`;
}

$("#applyBtn").addEventListener("click", async () => {
  const btn = $("#applyBtn");
  btn.disabled = true;
  btn.textContent = "Applying…";
  try {
    const columns = {};
    $all("#suggestTable tbody tr").forEach(tr => {
      const col = tr.dataset.column;
      const cfg = {};
      tr.querySelectorAll("select[data-field]").forEach(sel => {
        const field = sel.dataset.field;
        const val = sel.value;
        if (field === "missing") cfg.missing_strategy = val;
        if (field === "outlier") cfg.outlier_action = val;
        if (field === "encode") cfg.encode = val;
        if (field === "scale") cfg.scale = val;
      });
      columns[col] = cfg;
    });

    const body = { drop_duplicates: $("#dropDupCheckbox").checked, columns };
    const res = await api(`/api/preprocess/${state.sessionId}/${encodeURIComponent(state.currentPreprocessFile)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    await loadFinalPanel();
    showStep("final");
  } catch (err) {
    alert("Preprocessing failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Apply →";
  }
});

// ---------------------------------------------------------------- STEP 5: final
async function loadFinalPanel() {
  const sessionData = await api(`/api/session/${state.sessionId}`);
  fillSelect($("#finalFileSelect"), sessionData.files.map(f => f.filename));
  $("#finalFileSelect").value = state.currentPreprocessFile;

  const logBox = $("#transformLog");
  logBox.innerHTML = "";
  for (const [fname, entries] of Object.entries(sessionData.log)) {
    const heading = document.createElement("div");
    heading.className = "file-heading";
    heading.textContent = `[${fname}]`;
    logBox.appendChild(heading);
    entries.forEach(e => {
      const line = document.createElement("div");
      line.textContent = "  " + e;
      if (e.startsWith("⚠")) line.classList.add("log-line-warn");
      logBox.appendChild(line);
    });
  }
}

$("#downloadDataBtn").addEventListener("click", () => {
  const fname = $("#finalFileSelect").value;
  window.open(`${API_BASE}/api/download/${state.sessionId}/${encodeURIComponent(fname)}`, "_blank");
});

$("#downloadLogBtn").addEventListener("click", () => {
  window.open(`${API_BASE}/api/download-log/${state.sessionId}`, "_blank");
});

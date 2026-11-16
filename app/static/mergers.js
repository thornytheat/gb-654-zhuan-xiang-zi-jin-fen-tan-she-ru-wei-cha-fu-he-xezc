/* M7 合并映射工作区：登记发布、中断恢复、预算采信、权重测算、台账追查。 */
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const show = (id, payload) => {
  document.getElementById(id).textContent = JSON.stringify(payload, null, 2);
};

async function call(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error ? `${data.error}: ${data.message || ""}` : `HTTP ${res.status}`);
    err.payload = data;
    throw err;
  }
  return data;
}

function formObject(form) {
  const out = {};
  new FormData(form).forEach((v, k) => { out[k] = String(v).trim(); });
  return out;
}

/* ① 登记并发布 */
document.getElementById("m7-register-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = formObject(e.target);
  const payload = {
    effective_batch: f.effective_batch,
    note: f.note,
    simulate_interrupt: e.target.simulate_interrupt.checked,
    entries: JSON.parse(f.entries),
  };
  if (f.expected_version) payload.expected_version = Number(f.expected_version);
  if (f.request_key) payload.request_key = f.request_key;
  try {
    const data = await call("/api/mergers/mappings/register", payload);
    show("m7-register-out", data);
  } catch (err) { show("m7-register-out", { error: err.message, detail: err.payload }); }
  loadVersions();
  loadTrace();
});

/* 中断恢复 */
document.getElementById("m7-recover").addEventListener("click", async () => {
  try { show("m7-register-out", await call("/api/mergers/mappings/recover", {})); }
  catch (err) { show("m7-register-out", { error: err.message, detail: err.payload }); }
  loadVersions();
  loadTrace();
});

/* ③ 预算采信 */
document.getElementById("m7-import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = formObject(e.target);
  const payload = { batch: f.batch, rows: JSON.parse(f.rows) };
  if (f.mapping_version_id) payload.mapping_version_id = f.mapping_version_id;
  if (f.request_key) payload.request_key = f.request_key;
  try { show("m7-import-out", await call("/api/mergers/budgets/import", payload)); }
  catch (err) { show("m7-import-out", { error: err.message, detail: err.payload }); }
  loadTrace();
});

/* ④ 权重测算 */
document.getElementById("m7-weight-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = formObject(e.target);
  const payload = {};
  if (f.batch) payload.batch = f.batch;
  if (f.mapping_version_id) payload.mapping_version_id = f.mapping_version_id;
  if (f.import_id) payload.import_id = f.import_id;
  try { show("m7-weight-out", await call("/api/mergers/weights/calculate", payload)); }
  catch (err) { show("m7-weight-out", { error: err.message, detail: err.payload }); }
});

/* ② 版本台账 */
async function loadVersions() {
  const tbody = document.getElementById("m7-versions");
  try {
    const data = await (await fetch("/api/mergers/mappings")).json();
    if (!data.versions.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">暂无版本</td></tr>';
      return;
    }
    tbody.innerHTML = data.versions.map((v) => `
      <tr><td>v${v.version}<br><small>${esc(v.id)}</small></td>
      <td>${esc(v.effective_batch)}</td>
      <td><span class="sev ${v.status === "ACTIVE" ? "sev--ok" : v.status === "INTERRUPTED" ? "sev--warn" : ""}">${esc(v.status)}</span></td>
      <td class="num">${v.valid_count} / ${v.quarantine_count}</td>
      <td><small>${esc(v.digest.slice(0, 12))}</small></td></tr>`).join("");
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5">${esc(err.message)}</td></tr>`;
  }
}

/* ⑤ 追查：合并前后归属、权重、隔离 */
function ledgerTable(ledger) {
  if (!ledger.length) return '<p class="muted">无有效台账行</p>';
  return `<table class="ledger"><thead><tr><th>原编码</th><th>认领主体</th><th>新编码</th>
  <th>金额</th><th>权重</th><th>封顶</th><th>封顶来源</th><th>来源</th></tr></thead><tbody>
  ${ledger.map((r) => `<tr><td>${esc(r.project_id)}</td><td>${esc(r.claimed_entity)}</td>
  <td>${esc(r.mapped_new_code)}</td><td class="num">${esc(r.amount)}</td>
  <td class="num">${esc(r.weight)}</td><td class="num">${esc(r.cap)}</td>
  <td>${esc(r.cap_source)}</td><td>${r.is_legacy ? "旧补传" : "新申报"}</td></tr>`).join("")}
  </tbody></table>`;
}

async function loadTrace() {
  const box = document.getElementById("m7-trace");
  try {
    const data = await (await fetch("/api/mergers/trace")).json();
    const interrupted = data.interrupted.length
      ? `<p class="warn">存在中断版本：${data.interrupted.map((v) => esc(v.id)).join("，")}，请先恢复</p>` : "";
    const qMap = data.mapping_quarantine.length
      ? `<details open><summary>映射登记隔离（${data.mapping_quarantine.length} 条，不占有效合计）</summary>
      <table class="ledger"><thead><tr><th>行号</th><th>业务键</th><th>原因码</th></tr></thead><tbody>
      ${data.mapping_quarantine.map((q) => `<tr><td>${q.source_line}</td><td>${esc(q.business_key)}</td><td>${esc(q.reason)}</td></tr>`).join("")}
      </tbody></table></details>` : "";
    const imports = data.imports.map((d) => `
      <details open><summary>导入 ${esc(d.import.id)} · 批次 ${esc(d.import.batch)}
      · 绑定 ${esc(d.import.mapping_version_id || "无")}
      · 有效合计 <b>${esc(d.import.valid_total)}</b>
      · 有效/隔离 ${d.import.valid_count}/${d.import.quarantine_count}</summary>
      ${ledgerTable(d.ledger)}
      ${d.quarantine.length ? `<table class="ledger"><thead><tr><th>隔离行号</th><th>编码</th><th>原因码</th></tr></thead><tbody>
      ${d.quarantine.map((q) => `<tr><td>${q.source_line}</td><td>${esc(q.business_key)}</td><td>${esc(q.reason)}</td></tr>`).join("")}
      </tbody></table>` : ""}</details>`).join("") || '<p class="muted">暂无采信记录</p>';
    box.innerHTML = interrupted + qMap + imports;
  } catch (err) {
    box.textContent = err.message;
  }
}

document.getElementById("m7-refresh").addEventListener("click", () => {
  loadVersions(); loadTrace();
});
loadVersions();
loadTrace();

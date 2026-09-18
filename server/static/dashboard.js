/* Monitoring dashboard logic.
 * - keeps existing architecture: /api/latest, /api/machines, /api/roles, /api/process-catalog, etc.
 */

(function () {
  // Tab routing
  const TABS = ["monitoring", "analytics", "graphs", "settings"];

  function getActiveTab() {
    const btn = document.querySelector(".tabbtn.active");
    return btn ? btn.getAttribute("data-tab") : "monitoring";
  }

  function setActiveTab(name) {
    TABS.forEach(t => {
      const b = document.querySelector(`.tabbtn[data-tab="${t}"]`);
      const el = document.getElementById(`tab-${t}`);
      if (b) b.classList.toggle("active", t === name);
      if (el) el.classList.toggle("active", t === name);
    });
  }

  document.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest(".tabbtn") : null;
    if (!btn) return;
    const tab = btn.getAttribute("data-tab");
    if (!tab) return;
    setActiveTab(tab);
    if (tab === "analytics") renderAnalytics();
    if (tab === "graphs") renderGraphs();
    if (tab === "settings") renderSettings();
  });

  // API helpers
  async function apiGetJson(url) {
    const resp = await fetch(url, {
      headers: { "X-API-Key": localStorage.getItem("api_key") || "" }
    });
    if (resp.status === 401) throw new Error("401 Unauthorized (проверь localStorage api_key)");

    if (!resp.ok) {
      let t = "";
      try { t = await resp.text(); } catch {}
      const extra = t ? (": " + t.slice(0, 800)) : "";
      throw new Error(`HTTP ${resp.status} for ${url}${extra}`);
    }

    return await resp.json();

  }

  async function apiPostJson(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": localStorage.getItem("api_key") || ""
      },
      body: JSON.stringify(body)
    });
    if (resp.status === 401) throw new Error("401 Unauthorized (проверь localStorage api_key)");
    if (!resp.ok) {
      const t = await resp.text();
      throw new Error(`HTTP ${resp.status} for ${url}: ${t}`);
    }
    return await resp.json().catch(() => ({}));
  }


  // Process catalog cache (type/description)
  let PROCESS_CATALOG = { byName: {} };

  // CPU delta cache between refreshes (front-only, no DB/API changes)
  let PREV_CPU_BY_KEY = {}; // key -> { u, s, sample_time }

  function normProcName(s) {
    return (s || "").toString().trim().toLowerCase();
  }

  function cpuDeltaKey(ev, machineName) {
    const m = (machineName != null) ? String(machineName) : "";
    const pid = (ev && ev.pid != null) ? String(ev.pid) : "";
    const st = (ev && ev.start_time) ? String(ev.start_time) : "";
    const pn = normProcName(ev && ev.process_name);
    return `${m}|${pid}|${st}|${pn}`;
  }


  function computeCpuDeltas(latestMachines) {
    const next = {};

    for (const m of (latestMachines || [])) {
      // running_main is the main target (stopped is best-effort but may not have prev)
      for (const ev of (m.running_main || [])) {
        const k = cpuDeltaKey(ev, m.machine_name);
       const curU = (ev && ev.cpu_user_time_s != null) ? Number(ev.cpu_user_time_s) : null;
      const curS = (ev && ev.cpu_system_time_s != null) ? Number(ev.cpu_system_time_s) : null;

      // у running_main часто нет ev.sample_time -> используем sample_time машины как метку среза
      const curStamp = (ev && ev.sample_time) ? String(ev.sample_time) : String(m.sample_time || "");

      const prev = PREV_CPU_BY_KEY[k];
      let dU = null, dS = null;

      // считаем дельту, если уже есть prev (метку среза используем только как "антидубль")
      if (prev && prev.sample_time !== curStamp) {
        if (curU != null && isFinite(curU) && prev.u != null && isFinite(prev.u)) dU = Math.max(0, curU - prev.u);
        if (curS != null && isFinite(curS) && prev.s != null && isFinite(prev.s)) dS = Math.max(0, curS - prev.s);
      }

      ev._cpu_delta_user = dU;
      ev._cpu_delta_sys = dS;

      next[k] = { u: curU, s: curS, sample_time: curStamp };

      }

      // optional: also cache stopped detail items for potential reuse later (no display guarantee)
      for (const g of (m.stopped_groups || [])) {
        for (const ev of (g.items || [])) {
          const k = cpuDeltaKey(ev, m.machine_name);
          const curU = (ev && ev.cpu_user_time_s != null) ? Number(ev.cpu_user_time_s) : null;
          const curS = (ev && ev.cpu_system_time_s != null) ? Number(ev.cpu_system_time_s) : null;
          const curStamp = (ev && ev.sample_time) ? String(ev.sample_time) : String(m.sample_time || "");
          next[k] = { u: curU, s: curS, sample_time: curStamp };
        }
      }
    }

    PREV_CPU_BY_KEY = next;
  }

   function fmtCpuDelta(ev) {
    // Prefer server-computed deltas (consistent across devices)
    const du_srv = ev && ev.cpu_delta_user_s;
    const ds_srv = ev && ev.cpu_delta_system_s;
    if (du_srv != null || ds_srv != null) {
      const u = (du_srv == null) ? "—" : Number(du_srv).toFixed(1);
      const s = (ds_srv == null) ? "—" : Number(ds_srv).toFixed(1);
      return `${u}/${s}`;
    }

    // Fallback: old front-only cache delta (if still present)
    const du = ev && ev._cpu_delta_user;
    const ds = ev && ev._cpu_delta_sys;
    if (du == null && ds == null) return "—";
    const u = (du == null) ? "—" : Number(du).toFixed(1);
    const s = (ds == null) ? "—" : Number(ds).toFixed(1);
    return `${u}/${s}`;
  }



  function setProcessCatalog(items) {
    const byName = {};
    for (const it of (items || [])) {
      const k = normProcName(it.process_name);
      if (!k) continue;
      byName[k] = { process_type: (it.process_type || it.type || ""), description: (it.description || "") };
    }
    PROCESS_CATALOG.byName = byName;
  }


  async function ensureProcessCatalogLoaded() {
    try {
      // Load once per page session; reuse existing endpoint /api/process-catalog
      if (PROCESS_CATALOG && PROCESS_CATALOG._loaded) return;
      const data = await apiGetJson("/api/process-catalog");
      setProcessCatalog((data && data.items) ? data.items : []);
      PROCESS_CATALOG._loaded = true;
    } catch (e) {
      // Don't break Monitoring if catalog endpoint fails
      PROCESS_CATALOG._loaded = true;
    }
  }

function getProcType(name) {
    const k = normProcName(name);
    return (PROCESS_CATALOG.byName[k] && PROCESS_CATALOG.byName[k].process_type)
      ? PROCESS_CATALOG.byName[k].process_type
      : "—";
  }

  function getProcDesc(name) {
    const k = normProcName(name);
    return (PROCESS_CATALOG.byName[k] && PROCESS_CATALOG.byName[k].description)
      ? PROCESS_CATALOG.byName[k].description
      : "—";
  }

  function isKnownProcName(name) {
    const k = normProcName(name);
    if (!k) return false;
    return !!(PROCESS_CATALOG && PROCESS_CATALOG.byName && PROCESS_CATALOG.byName[k]);
  }

    function getSelectedTypes() {
    try { return JSON.parse(localStorage.getItem("type_filter") || "[]") || []; }
    catch { return []; }
  }

  function setSelectedTypes(arr) {
    localStorage.setItem("type_filter", JSON.stringify(arr || []));
  }

  function collectAllTypesFromCatalog() {
    const set = new Set();
    const byName = (PROCESS_CATALOG && PROCESS_CATALOG.byName) ? PROCESS_CATALOG.byName : {};
    for (const k of Object.keys(byName)) {
      const t = (byName[k].process_type || byName[k].type || "").toString().trim();
      if (t) set.add(t);
    }
    return Array.from(set).sort((a, b) => norm(a).localeCompare(norm(b), "ru"));
  }

  function renderTypeFilterUI() {
    const box = byId("typeFilterList");
    if (!box) return;

    const types = collectAllTypesFromCatalog();
    const selectedNorm = new Set(getSelectedTypes().map(x => norm((x || "").toString())));

    if (!types.length) {
      box.innerHTML = `<div class="muted">типов нет (проверь справочник процессов)</div>`;
      return;
    }

    box.innerHTML = types.map(t => {
      const esc = escapeHtml(t);
      const checked = selectedNorm.has(norm(t)) ? "checked" : "";
      return `<label class="chk" style="display:flex; margin:4px 0;">
        <input type="checkbox" data-type="${esc}" ${checked}> ${esc}
      </label>`;
    }).join("");

    box.querySelectorAll('input[type="checkbox"][data-type]').forEach(cb => {
      cb.addEventListener("change", () => {
        const now = [];
        box.querySelectorAll('input[type="checkbox"][data-type]').forEach(x => {
          if (x.checked) now.push(x.getAttribute("data-type"));
        });
        setSelectedTypes(now);
        fetchLatest();
      });
    });
  }



  // Column catalog
  const PROC_COLS = {
    type: {
      title: "Тип",
      cell: (ev) => `<td class="muted">${escapeHtml(getProcType(ev.process_name))}</td>`
    },
    desc: {
      title: "Описание",
      cell: (ev) => `<td class="muted">${escapeHtml(getProcDesc(ev.process_name))}</td>`
    },
    user: {
      title: "Пользователь",
      cell: (ev) => `<td>${escapeHtml(normUsername(ev.user_name) || "—")}</td>`
    },
    start: {
      title: "Начало",
      cell: (ev) => `<td class="muted">${escapeHtml(fmtLocalTs(ev.start_time || ""))}</td>`
    },
    end: {
      title: "Конец",
      cell: (ev) => `<td class="muted">${escapeHtml(fmtLocalTs(ev.end_time || ev.sample_time || ""))}</td>`
    },
    duration: {
      title: "Длительность",
      cell: (ev) => `<td>${escapeHtml(fmtDuration(ev.duration_seconds))}</td>`
    },
    pid: {
      title: "PID",
      cell: (ev) => `<td class="muted">${escapeHtml(ev.pid ?? "—")}</td>`
    },
    ppid: {
      title: "PPID",
      cell: (ev) => `<td class="muted">${escapeHtml(ev.ppid ?? "—")}</td>`
    },
    path: {
      title: "Путь",
      cell: (ev) => `<td class="muted">${escapeHtml(ev.exe_path || "—")}</td>`
    },
    cpu: {
      title: "CPU (u/s)",
      cell: (ev) => `<td class="muted">${escapeHtml(fmtCpu(ev))}</td>`
    },
    cpu_delta: {
      title: "CPU Δ",
      cell: (ev) => `<td class="muted">${escapeHtml(fmtCpuDelta(ev))}</td>`
    },
    rss: {
      title: "RSS",
      cell: (ev) => `<td class="muted">${escapeHtml(fmtBytes(ev.rss_bytes))}</td>`
    },
    io: {
      title: "IO",
      cell: (ev) => `<td class="muted">${escapeHtml(fmtIo(ev))}</td>`
    },
    net: {
      title: "Net",
      cell: (ev) => `<td class="muted">${escapeHtml(fmtNet(ev))}</td>`
    },
	sha256: {
		title: "SHA256",
		cell: (ev) => `<td class="mono">${escapeHtml((ev && ev.sha256) ? String(ev.sha256) : "—")}</td>`
	},
  };

  function getSelectedProcCols() {
    const wrap = document.body;
    const cols = Array.from(wrap.querySelectorAll('input[type="checkbox"][data-col]'))
      .filter(cb => cb.checked)
      .map(cb => cb.dataset.col);
    return cols.length ? cols : ["user", "start", "duration", "pid"];
  }

  // Filtering and sorting
  function norm(s) {
    return (s || "").toString().trim().toLowerCase();
  }

  function byMachineName(a, b) {
    return norm(a.machine_name).localeCompare(norm(b.machine_name), "ru");
  }

  function byLastSeen(a, b) {
    const ta = Date.parse(a.sample_time || "") || 0;
    const tb = Date.parse(b.sample_time || "") || 0;
    return ta - tb;
  }

  function applyMachineFilters(items) {
    const procSel = byId("procSel");
    const procStateSel = byId("procStateSel");
    const statusSel = byId("statusSel");
    const sortSel = byId("sortSel");


    let out = items.slice();

    const proc = procSel ? procSel.value : "";
    const pstate = procStateSel ? procStateSel.value : "all";
    const state = procStateSel ? procStateSel.value : "all";
    const status = statusSel ? statusSel.value : "all";
    const sort = sortSel ? sortSel.value : "machine_az";

    if (proc) {
      const p = norm(proc);
      out = out.filter(m => {
        const running = (m.running_main || []).some(x => norm(x.process_name) === p);
        const stopped = (m.stopped_groups || []).some(g => norm(g.process_name) === p);
        if (pstate === "running") return running;
        if (pstate === "stopped") return stopped;
        return running || stopped;
      });
    }


    if (state === "running") out = out.filter(m => (m.running_main || []).length > 0);
    if (state === "stopped") out = out.filter(m => (m.stopped_groups || []).length > 0);

    if (status === "online") out = out.filter(m => !!m.online);
    if (status === "offline") out = out.filter(m => !m.online);

    if (sort === "machine_az") out.sort(byMachineName);
    if (sort === "machine_za") out.sort((a, b) => -byMachineName(a, b));
    if (sort === "last_old") out.sort(byLastSeen);
    if (sort === "last_new") out.sort((a, b) => -byLastSeen(a, b));

        // Filter by process type (from process_catalog)
    const selectedTypes = getSelectedTypes();
    if (selectedTypes && selectedTypes.length) {
      // normalize types to lower-case to avoid case/space issues
      const sel = new Set(selectedTypes.map(x => norm((x || "").toString())));

      function typeOfProcName(pname) {
        const k = normProcName(pname);
        const rec = (PROCESS_CATALOG && PROCESS_CATALOG.byName) ? PROCESS_CATALOG.byName[k] : null;
        return rec ? norm((rec.process_type || rec.type || "").toString()) : "";
      }

      out = out.filter(m => {
        const names = [];
        if (pstate === "running" || pstate === "all") {
          for (const r of (m.running_main || [])) names.push(r.process_name || "");
        }
        if (pstate === "stopped" || pstate === "all") {
          for (const g of (m.stopped_groups || [])) names.push(g.process_name || "");
        }
        for (const n of names) {
          const t = typeOfProcName(n);
          if (t && sel.has(t)) return true;
        }
        return false;
      });

      // additionally filter processes inside each machine (so only selected types remain visible)
      out.forEach(m => {
        const keep = (pname) => {
          const t = typeOfProcName(pname);
          return t && sel.has(t);
        };

        if (Array.isArray(m.running_main)) {
          m.running_main = m.running_main.filter(ev => keep(ev.process_name || ""));
        }

        if (Array.isArray(m.stopped_groups)) {
          m.stopped_groups = m.stopped_groups
            .filter(g => keep(g.process_name || ""))
            .map(g => {
              if (Array.isArray(g.items)) {
                g.items = g.items.filter(ev => keep(ev.process_name || g.process_name || ""));
              }
              return g;
            });
        }
      });



    }

    return out;
  }

  // Rendering: Monitoring tab

  function buildProcSelect(latest) {
    const procSel = byId("procSel");
    if (!procSel) return;

    const cur = procSel.value || "";
    const set = new Set();
    const procStateSel = byId("procStateSel");
    const pstate = procStateSel ? procStateSel.value : "all";
    for (const m of (latest || [])) {
      if (pstate === "running" || pstate === "all") {
        for (const r of (m.running_main || [])) set.add(r.process_name || "");
      }
      if (pstate === "stopped" || pstate === "all") {
        for (const g of (m.stopped_groups || [])) set.add(g.process_name || "");
      }
    }

    const items = Array.from(set).filter(Boolean).sort((a, b) => norm(a).localeCompare(norm(b), "ru"));
    procSel.innerHTML = `<option value="">(все)</option>` + items.map(p => {
      const esc = escapeHtml(p);
      return `<option value="${esc}">${esc}</option>`;
    }).join("");

    procSel.value = cur;
  }

  function renderProcTable(events) {
    const cols = getSelectedProcCols();
    const table = document.createElement("table");

    const thead = document.createElement("thead");
    thead.innerHTML = `<tr><th>Process</th>${cols.map(c => `<th>${escapeHtml(PROC_COLS[c]?.title || c)}</th>`).join("")}</tr>`;
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const ev of (events || [])) {
      const tr = document.createElement("tr");
      if (!isKnownProcName(ev.process_name)) tr.classList.add("unknownProc");
      const pname = ev.process_name || "unknown";
      tr.innerHTML = `<td><b>${escapeHtml(pname)}</b>${isKnownProcName(pname) ? "" : '<span class="unknownMark">!</span>'}</td>` +
        cols.map(c => (PROC_COLS[c] ? PROC_COLS[c].cell(ev) : `<td>—</td>`)).join("");
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    return table;
  }

  function renderRunningBlock(machine) {
    const wrap = document.createElement("div");
    const running = machine.running_main || [];

    const sortSel = document.createElement("select");
    sortSel.style.marginLeft = "6px";
    sortSel.innerHTML = `
      <option value="name_az">Имя A→Z</option>
      <option value="name_za">Имя Z→A</option>
      <option value="user_az">Пользователь A→Z</option>
      <option value="user_za">Пользователь Z→A</option>
      <option value="start_new">Начало (новые)</option>
      <option value="start_old">Начало (старые)</option>
      <option value="dur_desc">Длительность (desc)</option>
      <option value="dur_asc">Длительность (asc)</option>
      <option value="cpu_desc">CPU (u/s) (desc)</option>
      <option value="cpu_asc">CPU (u/s) (asc)</option>
      <option value="cpuD_desc">CPU Δ (desc)</option>
      <option value="cpuD_asc">CPU Δ (asc)</option>
      <option value="rss_desc">RSS (desc)</option>
      <option value="rss_asc">RSS (asc)</option>
      <option value="io_desc">IO (desc)</option>
      <option value="io_asc">IO (asc)</option>
      <option value="net_desc">Net (desc)</option>
      <option value="net_asc">Net (asc)</option>

    `;

    const head = document.createElement("div");
    head.className = "badge";
    head.style.marginBottom = "10px";
    head.innerHTML = `<b>Запущенные</b> <span class="muted">(${running.length})</span> <span class="muted">сортировка:</span>`;
    head.appendChild(sortSel);
    wrap.appendChild(head);

    function sortArr(arr, mode) {
      const num = (x) => (x == null || !isFinite(Number(x))) ? null : Number(x);

      const cpuSum = (ev) => (num(ev.cpu_user_time_s) || 0) + (num(ev.cpu_system_time_s) || 0);

      const cpuDSum = (ev) => {
        const u = num(ev._cpu_delta_user);
        const s = num(ev._cpu_delta_sys);
        if (u == null && s == null) return null;
        return (u || 0) + (s || 0);
      };

      const ioSum = (ev) => (num(ev.io_read_bytes) || 0) + (num(ev.io_write_bytes) || 0);

      const byName = (a, b) => norm(a.process_name).localeCompare(norm(b.process_name), "ru");
      const byUser = (a, b) => norm(normUsername(a.user_name)).localeCompare(norm(normUsername(b.user_name)), "ru");
      const byStart = (a, b) => (Date.parse(a.start_time || "") || 0) - (Date.parse(b.start_time || "") || 0);
      const byDur = (a, b) => (Number(a.duration_seconds) || 0) - (Number(b.duration_seconds) || 0);

      switch (mode) {
        case "name_az": return arr.sort(byName);
        case "name_za": return arr.sort((a, b) => -byName(a, b));
        case "user_az": return arr.sort(byUser);
        case "user_za": return arr.sort((a, b) => -byUser(a, b));
        case "start_new": return arr.sort((a, b) => -byStart(a, b));
        case "start_old": return arr.sort(byStart);
        case "dur_desc": return arr.sort((a, b) => -byDur(a, b));
        case "dur_asc": return arr.sort(byDur);
        case "cpu_desc": return arr.sort((a,b)=> (cpuSum(b) ?? -1) - (cpuSum(a) ?? -1));
        case "cpu_asc": return arr.sort((a,b)=> (cpuSum(a) ?? 1e18) - (cpuSum(b) ?? 1e18));
        case "cpuD_desc": return arr.sort((a,b)=> (cpuDSum(b) ?? -1) - (cpuDSum(a) ?? -1));
        case "cpuD_asc": return arr.sort((a,b)=> (cpuDSum(a) ?? 1e18) - (cpuDSum(b) ?? 1e18));
        case "rss_desc": return arr.sort((a,b)=> (num(b.rss_bytes) ?? -1) - (num(a.rss_bytes) ?? -1));
        case "rss_asc": return arr.sort((a,b)=> (num(a.rss_bytes) ?? 1e18) - (num(b.rss_bytes) ?? 1e18));
        case "io_desc": return arr.sort((a,b)=> (ioSum(b) ?? -1) - (ioSum(a) ?? -1));
        case "io_asc": return arr.sort((a,b)=> (ioSum(a) ?? 1e18) - (ioSum(b) ?? 1e18));
        case "net_desc": return arr.sort((a,b)=> (num(b.net_conn_count) ?? -1) - (num(a.net_conn_count) ?? -1));
        case "net_asc": return arr.sort((a,b)=> (num(a.net_conn_count) ?? 1e18) - (num(b.net_conn_count) ?? 1e18));
        default: return arr.sort((a, b) => -byStart(a, b));
      }
    }

    function paintTable() {
      const existing = wrap.querySelector("div.tableWrap");
      if (existing) existing.remove();

      const arr = sortArr(running.slice(), sortSel.value);
      const table = renderProcTable(arr);

      const tw = document.createElement("div");
      tw.className = "tableWrap";
      tw.appendChild(table);
      wrap.appendChild(tw);
    }

    sortSel.addEventListener("change", paintTable);

    if (!running.length) {
      const table = renderProcTable([]);
      const tw = document.createElement("div");
      tw.className = "tableWrap";
      tw.appendChild(table);
      wrap.appendChild(tw);
      return wrap;
    }

    paintTable();
    return wrap;
  }


  function renderStoppedBlock(machine) {
    const wrap = document.createElement("div");
    let groups = machine.stopped_groups || [];

    // sorting for groups (minimal, predictable)
    const sortSel = document.createElement("select");
    sortSel.style.marginLeft = "6px";
    sortSel.innerHTML = `
      <option value="name_az">Имя A→Z</option>
      <option value="name_za">Имя Z→A</option>
      <option value="dur_desc">Общее время (desc)</option>
      <option value="dur_asc">Общее время (asc)</option>
      <option value="cnt_desc">Кол-во (desc)</option>
      <option value="cnt_asc">Кол-во (asc)</option>
    `;

    const head = document.createElement("div");
    head.className = "badge";
    head.style.marginBottom = "10px";
    head.innerHTML = `<b>Завершённые</b> <span class="muted">(${groups.length})</span> <span class="muted">сортировка:</span>`;
    head.appendChild(sortSel);
    wrap.appendChild(head);

    function sortGroups(arr, mode) {
      const byName = (a, b) => norm(a.process_name).localeCompare(norm(b.process_name), "ru");
      const byDur = (a, b) => (Number(a.total_duration_seconds) || 0) - (Number(b.total_duration_seconds) || 0);
      const byCnt = (a, b) => (Number(a.count) || 0) - (Number(b.count) || 0);
      switch (mode) {
        case "name_az": return arr.sort(byName);
        case "name_za": return arr.sort((a, b) => -byName(a, b));
        case "dur_desc": return arr.sort((a, b) => -byDur(a, b));
        case "dur_asc": return arr.sort(byDur);
        case "cnt_desc": return arr.sort((a, b) => -byCnt(a, b));
        case "cnt_asc": return arr.sort(byCnt);
        default: return arr.sort(byName);
      }
    }

    const table = document.createElement("table");
    table.innerHTML = `
      <thead>
        <tr>
          <th>Процесс</th>
          <th>Общее время</th>
          <th>Кол-во</th>
          <th>Детали</th>
        </tr>
      </thead>
    `;

    const tbody = document.createElement("tbody");

    function paint() {
      tbody.innerHTML = "";
      const arr = sortGroups(groups.slice(), sortSel.value);

      for (const g of arr) {
        const tr = document.createElement("tr");

        const pnameG = (g.process_name || "unknown");
        if (!isKnownProcName(pnameG)) tr.classList.add("unknownProc");

        const tdName = document.createElement("td");
        tdName.innerHTML = `<b>${escapeHtml(pnameG)}</b>${isKnownProcName(pnameG) ? "" : '<span class="unknownMark">!</span>'}`;
        tr.appendChild(tdName);

        const tdDur = document.createElement("td");
        tdDur.className = "muted";
        tdDur.textContent = fmtDuration(g.total_duration_seconds);
        tr.appendChild(tdDur);

        const tdCnt = document.createElement("td");
        tdCnt.className = "muted";
        tdCnt.textContent = String(g.count ?? "");
        tr.appendChild(tdCnt);

        const tdDet = document.createElement("td");
        const det = document.createElement("details");
        const sum = document.createElement("summary");
        sum.className = "muted";
        sum.textContent = `Показать (${g.count || 0})`;
        det.appendChild(sum);
        tdDet.appendChild(det);
        tr.appendChild(tdDet);

        det.addEventListener("toggle", () => {
          if (!det.open) return;
          if (det.querySelector("div._inner")) return;

          const innerWrap = document.createElement("div");
          innerWrap.className = "_inner";
          innerWrap.style.marginTop = "8px";

          const inHead = document.createElement("div");
          inHead.style.display = "flex";
          inHead.style.alignItems = "center";
          inHead.style.gap = "8px";
          inHead.style.marginBottom = "6px";

          const lbl = document.createElement("span");
          lbl.className = "muted";
          lbl.textContent = "Сортировка:";
          inHead.appendChild(lbl);

          const sortInner = document.createElement("select");
          sortInner.innerHTML = `
            <option value="start_new">Начало (новые)</option>
            <option value="start_old">Начало (старые)</option>
            <option value="dur_desc">Длительность (desc)</option>
            <option value="dur_asc">Длительность (asc)</option>
            <option value="cpu_desc">CPU (u/s) (desc)</option>
            <option value="cpu_asc">CPU (u/s) (asc)</option>
            <option value="cpuD_desc">CPU Δ (desc)</option>
            <option value="cpuD_asc">CPU Δ (asc)</option>
            <option value="rss_desc">RSS (desc)</option>
            <option value="rss_asc">RSS (asc)</option>
            <option value="io_desc">IO (desc)</option>
            <option value="io_asc">IO (asc)</option>
            <option value="net_desc">Net (desc)</option>
            <option value="net_asc">Net (asc)</option>
          `;
          inHead.appendChild(sortInner);

          innerWrap.appendChild(inHead);

          const items = (g.items || []).slice();

          function sortProcItems(arr, mode) {
            const num = (x) => (x == null || !isFinite(Number(x))) ? null : Number(x);

            const cpuSum = (ev) => {
              const u = num(ev.cpu_user_time_s) || 0;
              const s = num(ev.cpu_system_time_s) || 0;
              return u + s;
            };
            const cpuDSum = (ev) => {
              const u = num(ev._cpu_delta_user);
              const s = num(ev._cpu_delta_sys);
              if (u == null && s == null) return null;
              return (u || 0) + (s || 0);
            };
            const rss = (ev) => num(ev.rss_bytes);
            const ioSum = (ev) => {
              const r = num(ev.io_read_bytes) || 0;
              const w = num(ev.io_write_bytes) || 0;
              return r + w;
            };
            const netC = (ev) => num(ev.net_conn_count);
            const st = (ev) => (ev.start_time || "");
            const dur = (ev) => num(ev.duration_seconds) || 0;

            function byKey(getter, dir) {
              return (a, b) => {
                const av = getter(a), bv = getter(b);
                if (av == null && bv == null) return 0;
                if (av == null) return 1;
                if (bv == null) return -1;
                return dir * (av - bv);
              };
            }

            switch (mode) {
              case "start_old": return arr.sort((a,b)=> (st(a) > st(b) ? 1 : st(a) < st(b) ? -1 : 0));
              case "start_new": return arr.sort((a,b)=> (st(a) > st(b) ? -1 : st(a) < st(b) ? 1 : 0));
              case "dur_asc": return arr.sort(byKey(dur, +1));
              case "dur_desc": return arr.sort(byKey(dur, -1));
              case "cpu_asc": return arr.sort(byKey(cpuSum, +1));
              case "cpu_desc": return arr.sort(byKey(cpuSum, -1));
              case "cpuD_asc": return arr.sort(byKey(cpuDSum, +1));
              case "cpuD_desc": return arr.sort(byKey(cpuDSum, -1));
              case "rss_asc": return arr.sort(byKey(rss, +1));
              case "rss_desc": return arr.sort(byKey(rss, -1));
              case "io_asc": return arr.sort(byKey(ioSum, +1));
              case "io_desc": return arr.sort(byKey(ioSum, -1));
              case "net_asc": return arr.sort(byKey(netC, +1));
              case "net_desc": return arr.sort(byKey(netC, -1));
              default: return arr;
            }
          }


          function paintInner() {
            const existing = innerWrap.querySelector("div.tableWrap");
            if (existing) existing.remove();

            const cols0 = getSelectedProcCols();
            const cols = [];
            for (const c of cols0) {
              cols.push(c);
              if (c === "start") cols.push("end");
            }
            if (!cols.includes("end")) cols.push("end");


            const sorted = sortProcItems(items.slice(), sortInner.value);

            const t = document.createElement("table");
            t.innerHTML = `<thead><tr><th>Process</th>${cols.map(c => `<th>${escapeHtml(PROC_COLS[c]?.title || c)}</th>`).join("")}</tr></thead>`;
            const tb = document.createElement("tbody");
            for (const ev of sorted) {
              const tr2 = document.createElement("tr");
              if (!isKnownProcName(ev.process_name)) tr2.classList.add("unknownProc");
              const pname2 = ev.process_name || "unknown";
              tr2.innerHTML = `<td><b>${escapeHtml(pname2)}</b>${isKnownProcName(pname2) ? "" : '<span class="unknownMark">!</span>'}</td>` +
                cols.map(c => (PROC_COLS[c] ? PROC_COLS[c].cell(ev) : `<td>—</td>`)).join("");
              tb.appendChild(tr2);
            }
            t.appendChild(tb);

            const tw = document.createElement("div");
            tw.className = "tableWrap";
            tw.appendChild(t);
            innerWrap.appendChild(tw);
          }

          paintInner();
          sortInner.addEventListener("change", paintInner);
          det.appendChild(innerWrap);
        });

        tbody.appendChild(tr);
      }
    }

    sortSel.addEventListener("change", paint);

    table.appendChild(tbody);
    const tw = document.createElement("div");
    tw.className = "tableWrap";
    tw.appendChild(table);
    wrap.appendChild(tw);

    paint();
    return wrap;
  }

  function renderMachineCard(item) {
    const card = document.createElement("div");
    card.className = "card";

    const head = document.createElement("div");
    head.className = "machineHeader";


    const meta = document.createElement("div");
    meta.className = "machineMeta";

    const alias = (item && item.alias) ? String(item.alias) : "";
    const titleLine = document.createElement("div");
    titleLine.innerHTML = alias
      ? `<b>${escapeHtml(item.machine_name || "unknown")}</b> <span class="muted" style="font-style:italic;">(${escapeHtml(alias)})</span>`
      : `<b>${escapeHtml(item.machine_name || "unknown")}</b>`;
    head.appendChild(titleLine);

    const sliceLine = document.createElement("div");
    sliceLine.className = "muted";
    sliceLine.textContent = "срез: " + fmtLocalTs(item.sample_time || "");
    meta.appendChild(sliceLine);

    const onlineLine = document.createElement("div");
    onlineLine.className = "muted";
    if (item.online) {
      onlineLine.innerHTML = '<span style="color: #0a7f2e; font-weight: 700;">🟢 Online</span> — online since: ' + escapeHtml(fmtLocalTs(item.boot_time || ""));
    } else {
      onlineLine.innerHTML = '<span style="color: #b00020; font-weight: 700;">🔴 Offline</span> — last seen: ' + escapeHtml(fmtLocalTs(item.sample_time || ""));
    }
    meta.appendChild(onlineLine);

    const osLine = document.createElement("div");
    osLine.className = "muted";
    osLine.textContent = "OS: " + (item.os_info || "—");
    meta.appendChild(osLine);

    const uLine = document.createElement("div");
    uLine.className = "muted";
    uLine.textContent = "user: " + (normUsername(item.current_user || item.user_name || "") || "—");
    meta.appendChild(uLine);

    const cLine = document.createElement("div");
    cLine.className = "muted";
    cLine.textContent = "client: " + (item.client_version || "—");
    meta.appendChild(cLine);

    if (item.client_outdated) {
      const warn = document.createElement("div");
      warn.className = "muted";
      warn.style.fontWeight = "700";
      warn.textContent = `client outdated: ${item.client_version || ""} → ${item.latest_client_version || ""}`;
      meta.appendChild(warn);
    }

    head.appendChild(meta);
    card.appendChild(head);

    const det = document.createElement("details");
    det.className = "machineDetails";
    const sum = document.createElement("summary");
    sum.innerHTML = `<span class="hint">Показать детали</span>`;
    det.appendChild(sum);

    // Toggle details by clicking on card header (free space)
    head.style.cursor = "pointer";
    head.addEventListener("click", (e) => {
      if (e && e.target && e.target.closest) {
        if (e.target.closest("button, a, input, select, textarea, summary")) return;
      }
      det.open = !det.open;
    });

    det.addEventListener("toggle", () => {
      if (!det.open) return;
      if (det.querySelector("div._built")) return;

      const built = document.createElement("div");
      built.className = "_built";

      // Running + Stopped blocks
      const procStateSel = byId("procStateSel");
      const pstate = procStateSel ? procStateSel.value : "all";

      if (pstate === "running" || pstate === "all") {
        built.appendChild(renderRunningBlock(item));
      }
      if (pstate === "all") {
        built.appendChild(document.createElement("div")).style.height = "10px";
      }
      if (pstate === "stopped" || pstate === "all") {
        built.appendChild(renderStoppedBlock(item));
      }


      det.appendChild(built);
    });

    card.appendChild(det);
    return card;
  }

  async function fetchLatest() {
    const status = document.getElementById("status");
    const error = document.getElementById("error");
    const cards = document.getElementById("cards");

    if (error) error.textContent = "";
    if (status) status.textContent = "Загрузка...";

    try {
      const [data] = await Promise.all([apiGetJson("/api/latest"), ensureProcessCatalogLoaded()]);
      renderTypeFilterUI();
      const latest = data.latest || [];
      buildProcSelect(latest);

      if (status) status.textContent = "Последнее обновление: " + new Date().toLocaleString();

      if (cards) {
        cards.innerHTML = "";
        const filtered = applyMachineFilters(latest);
        for (const item of filtered) cards.appendChild(renderMachineCard(item));
      }
    } catch (e) {
      if (status) status.textContent = "";
      if (error) error.textContent = (e && e.message) ? e.message : String(e);
    }
  }

    // initial and controls
    document.addEventListener("DOMContentLoaded", () => {
      const refreshBtn = document.getElementById("refreshBtn");
      if (refreshBtn) refreshBtn.addEventListener("click", () => location.reload());

      const procSel = document.getElementById("procSel");
      const procStateSel = document.getElementById("procStateSel");
      const statusSel = document.getElementById("statusSel");
      const sortSel = document.getElementById("sortSel");

      [procSel, procStateSel, statusSel, sortSel].forEach(el => {
        if (el) el.addEventListener("change", fetchLatest);
      });
    });


    document.querySelectorAll('input[type="checkbox"][data-col]').forEach(cb => {
      cb.addEventListener("change", () => {
        // close open details so rerender doesn't keep stale content
        document.querySelectorAll("details[open]").forEach(d => d.open = false);
        fetchLatest();
      });
    });

    fetchLatest();

    const refreshSeconds = Number(window.REFRESH_SECONDS || 120);
    if (refreshSeconds > 0) setInterval(fetchLatest, refreshSeconds * 1000);


  // Helpers

  function byId(id) { return document.getElementById(id); }

  function normUsername(u) {
    const s = (u || "").toString().trim();
    if (!s) return "";
    const i = s.indexOf("\\\\");
    return (i >= 0) ? s.slice(i + 1) : s;
  }

  function fmtBinLabel(row, showSha) {
    const pn = (row && row.process_name) ? String(row.process_name) : "unknown.exe";
    const h = (row && row.sha256) ? String(row.sha256) : "";
    return (showSha && h) ? `${pn} (${h})` : pn;
  }

  function fmtChainLabel(row, showSha) {
    const p = (row && row.parent_process) ? String(row.parent_process) : "unknown";
    const c = (row && row.child_process) ? String(row.child_process) : "unknown";
    const ph = (row && row.parent_sha256) ? String(row.parent_sha256) : "";
    const ch = (row && row.child_sha256) ? String(row.child_sha256) : "";
    if (showSha && (ph || ch)) {
      const p2 = ph ? `${p} (${ph})` : p;
      const c2 = ch ? `${c} (${ch})` : c;
      return `${p2} → ${c2}`;
    }
    return `${p} → ${c}`;
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function fmtLocalTs(iso) {
    if (!iso) return "";
    try {
      const d = new Date(String(iso));
      if (isNaN(d.getTime())) return String(iso);
      return d.toLocaleString();
    } catch (e) {
      return String(iso);
    }
  }

  function fmtDuration(sec) {
    const s = Number(sec) || 0;
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = Math.floor(s % 60);
    if (h) return `${h}h ${m}m ${ss}s`;
    if (m) return `${m}m ${ss}s`;
    return `${ss}s`;
  }

  function fmtBytes(v) {
    const n = Number(v);
    if (!isFinite(n) || n < 0) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let x = n;
    let i = 0;
    while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
    return `${x.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  function fmtCpu(ev) {
    const u = ev.cpu_user_time_s;
    const s = ev.cpu_system_time_s;
    if (u == null && s == null) return "—";
    return `${Number(u || 0).toFixed(1)}/${Number(s || 0).toFixed(1)}`;
  }

  function fmtIo(ev) {
    const rb = ev.io_read_bytes;
    const wb = ev.io_write_bytes;
    if (rb == null && wb == null) return "—";
    return `${fmtBytes(rb || 0)} / ${fmtBytes(wb || 0)}`;
  }

  function fmtNet(ev) {
    const a = ev.net_active;
    const c = ev.net_conn_count;
    if (a == null && c == null) return "—";
    const active = (a === 1 || a === true) ? "yes" : "no";
    return `${active} (${c ?? "?"})`;
  }

  // Analytics tab

  function applyCollapsibleTables(scopeEl, maxRows = 2) {
    if (!scopeEl) return;

    const tables = Array.from(scopeEl.querySelectorAll("table"));
    tables.forEach((table, idx) => {
      const tbody = table.querySelector("tbody");
      if (!tbody) return;

      const rows = Array.from(tbody.querySelectorAll(":scope > tr"));
      if (rows.length <= maxRows) return;

      const tableId = table.dataset.collapseId || `tbl_${idx}_${Math.random().toString(36).slice(2, 8)}`;
      table.dataset.collapseId = tableId;

      const wrap = table.closest(".tableWrap") || table.parentElement;
      if (!wrap) return;

      const oldToggle = scopeEl.querySelector(`[data-collapse-toggle-for="${tableId}"]`);
      if (oldToggle) oldToggle.remove();

      let expanded = false;

      const applyState = () => {
        rows.forEach((row, i) => {
          row.style.display = (expanded || i < maxRows) ? "" : "none";
        });
        toggle.textContent = expanded ? "(Свернуть)" : "(Развернуть)";
      };

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.setAttribute("data-collapse-toggle-for", tableId);
      toggle.style.marginTop = "8px";
      toggle.style.padding = "2px 8px";
      toggle.style.borderRadius = "999px";
      toggle.style.border = "1px solid #ddd";
      toggle.style.cursor = "pointer";
      toggle.addEventListener("click", () => {
        expanded = !expanded;
        applyState();
      });

      wrap.insertAdjacentElement("afterend", toggle);
      applyState();
    });
  }

 async function renderAnalytics() {
  const root = byId("analyticsRoot");
  if (!root) return;

  root.innerHTML = `
    <h2 style="margin:0 0 10px 0;">Аналитика</h2>

    <div class="card" style="margin-bottom:12px;">
      <h3 style="margin-top:0;">Оповещения</h3>
      <div class="badge" style="display:flex; flex-wrap:wrap; gap:10px; align-items:center;">
        <select id="alPeriod">
          <option value="1h">1ч</option>
          <option value="24h">24ч</option>
          <option value="7d" selected>7д</option>
          <option value="30d">30д</option>
        </select>

        <select id="alStatus">
          <option value="">Статус: все</option>
          <option value="new">Новый</option>
          <option value="ack">В работе</option>
          <option value="closed">Закрыт</option>
        </select>

        <select id="alSeverity">
          <option value="">Уровень: все</option>
          <option value="low">Низкий</option>
          <option value="med">Средний</option>
          <option value="high">Высокий</option>
        </select>

        <select id="alMachine">
          <option value="">Машина: все</option>
        </select>

        <select id="alEntityType">
          <option value="">Сущность: все</option>
          <option value="process_session">сессия процесса</option>
          <option value="process_chain">цепочка процессов</option>
        </select>

        <select id="alRuleType">
          <option value="">Правило: все</option>
          <option value="rarity">редкость</option>
          <option value="chain">цепочка</option>
          <option value="time">время</option>
          <option value="resources">ресурсы</option>
          <option value="combined">комбинированное</option>
        </select>

        <button id="anRefresh" style="padding:6px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Обновить</button>
      </div>
    </div>

    <div id="anAlertsOut"></div>

    <div class="card" style="margin:12px 0;">
      <h3 style="margin-top:0;">Профили</h3>
      <div class="badge" style="display:flex; flex-wrap:wrap; gap:10px; align-items:center;">
        <label class="muted">Тип профиля:</label>
        <select id="anProfileType">
          <option value="machine">машина</option>
          <option value="process">процесс</option>
          <option value="chain">цепочка</option>
        </select>

        <label class="muted">Хост:</label>
        <select id="anHostSel"></select>

        <label class="muted">Процесс:</label>
        <select id="anProcessSel"><option value="">(выберите процесс)</option></select>

        <label class="muted">Цепочка:</label>
        <select id="anChainSel"><option value="">(выберите цепочку)</option></select>

        <label class="muted">Дней:</label>
        <input id="anDays" type="number" min="1" max="365" value="14" style="width:90px;" />
      </div>
    </div>

    <div id="anProfilesOut"></div>
  `;

  const alertsOut = byId("anAlertsOut");
  const profilesOut = byId("anProfilesOut");
  const alertMachineSel = byId("alMachine");
  const hostSel = byId("anHostSel");
  const procSel = byId("anProcessSel");
  const chainSel = byId("anChainSel");
  const profileTypeSel = byId("anProfileType");

  function isoSinceFromPeriod(p) {
    const now = Date.now();
    let ms = 7 * 24 * 3600 * 1000;
    if (p === "1h") ms = 3600 * 1000;
    if (p === "24h") ms = 24 * 3600 * 1000;
    if (p === "30d") ms = 30 * 24 * 3600 * 1000;
    return new Date(now - ms).toISOString();
  }

  function fmtScore(v) {
    if (v == null) return "—";
    const n = Number(v);
    return isFinite(n) ? n.toFixed(2) : "—";
  }

  function alertRuleGroup(a) {
    const m = (a.metric || "").toString();
    const reason = (a.reason || "").toString().toLowerCase();
    if (reason.includes("[combined]")) return "combined";
    if (m === "rarity") return "rarity";
    if (m === "chain_rarity" || m === "chain_anomaly" || m === "chain_depth_anomaly" || m === "chain_fanout_anomaly") return "chain";
    if (m === "time_anomaly") return "time";
    return "resources";
  }

  function alertEntityName(a) {
    if (a.entity_type === "process_chain") {
      return `${a.parent_process_name || "unknown"} -> ${a.process_name || ""}`;
    }
    return a.process_name || "";
  }

  async function loadHosts() {
    const m = await apiGetJson("/api/machines");
    const items = (m && m.items) ? m.items : [];

    const optionsHtml = `<option value="">(все)</option>` + items.map(x => {
      const name = x.machine_name || "";
      const alias = x.alias ? ` (${x.alias})` : "";
      return `<option value="${escapeHtml(name)}">${escapeHtml(name + alias)}</option>`;
    }).join("");

    hostSel.innerHTML = optionsHtml;
    if (alertMachineSel) {
      alertMachineSel.innerHTML = `<option value="">Машина: все</option>` + items.map(x => {
        const name = x.machine_name || "";
        const alias = x.alias ? ` (${x.alias})` : "";
        return `<option value="${escapeHtml(name)}">${escapeHtml(name + alias)}</option>`;
      }).join("");
    }
  }

  async function loadProcesses() {
    const data = await apiGetJson("/api/analytics/process-names?limit=200");
    const items = data.items || [];
    procSel.innerHTML = `<option value="">(выберите процесс)</option>` + items.map(x =>
      `<option value="${escapeHtml(x.process_name || "")}">${escapeHtml(x.process_name || "")}</option>`
    ).join("");
  }

  async function loadChainsList() {
    const days = Number(byId("anDays")?.value || 14) || 14;
    const data = await apiGetJson(`/api/analytics/chain-keys?days=${encodeURIComponent(days)}&limit=200`);
    const items = data.items || [];

    chainSel.innerHTML = `<option value="">(выберите цепочку)</option>` + items.map(x => {
      const label = `${x.parent_process_name || "unknown"} -> ${x.child_process_name || "unknown"}`;
      return `<option value="${escapeHtml(x.chain_key || "")}">${escapeHtml(label)}</option>`;
    }).join("");
  }

  const LAST_ALERTS_BY_ID = {};

  async function loadAlerts() {
    const q = new URLSearchParams();
    q.set("since", isoSinceFromPeriod(byId("alPeriod")?.value || "7d"));
    if (byId("alStatus")?.value) q.set("status", byId("alStatus").value);
    if (byId("alSeverity")?.value) q.set("severity", byId("alSeverity").value);
    if (byId("alEntityType")?.value) q.set("entity_type", byId("alEntityType").value);
    if (byId("alMachine")?.value) q.set("machine", byId("alMachine").value);
    q.set("limit", "200");
    q.set("offset", "0");

    const data = await apiGetJson(`/api/alerts?${q.toString()}`);
    let items = data.items || [];

    const ruleType = byId("alRuleType")?.value || "";
    if (ruleType) {
      items = items.filter(a => alertRuleGroup(a) === ruleType);
    }
    for (const a of items) LAST_ALERTS_BY_ID[String(a.id)] = a;

    return `
      <div class="card" style="margin-bottom:12px;">
        <h3 style="margin-top:0;">Оповещения</h3>
        <div class="muted" style="margin:-6px 0 10px 0;">Показано: ${items.length}${(data && data.total != null) ? ` / Всего: ${escapeHtml(data.total)}` : ""}</div>
        <div class="tableWrap"><table>
          <thead>
            <tr>
              <th>Время</th><th>Уровень</th><th>Сущность</th><th>Правило</th>
              <th>Процесс / цепочка</th><th>Пользователь</th><th>Машина</th>
              <th>Значение</th><th>Базовое значение</th><th>Оценка</th><th>Причина</th><th>Статус</th>
            </tr>
          </thead>
          <tbody>
            ${
              items.map(a => `
                <tr>
                  <td class="mono">${escapeHtml(fmtLocalTs(a.sample_time || ""))}</td>
                  <td><span class="pill ${(a.severity==='high') ? 'warn' : ((a.severity==='med') ? 'medwarn' : '')}">${escapeHtml(a.severity || "")}</span></td>
                  <td>${escapeHtml(a.entity_type || "")}</td>
                  <td>${escapeHtml(alertRuleGroup(a))}</td>
                  <td>${escapeHtml(alertEntityName(a))}</td>
                  <td>${escapeHtml(a.user_name || "")}</td>
                  <td>${escapeHtml(a.machine_name || "")}</td>
                  <td class="mono">${escapeHtml((a.value ?? "—").toString())}</td>
                  <td class="mono">${escapeHtml((a.baseline ?? "—").toString())}</td>
                  <td class="mono">${escapeHtml(fmtScore(a.score))}</td>
                  <td class="mono" style="max-width:520px; white-space:pre-wrap;">${escapeHtml(a.reason || "")}</td>
                  <td data-alert-status-cell="1" data-alert-id="${escapeHtml(String(a.id || ""))}">
                    <span data-alert-status-text="1">${escapeHtml(a.status || "")}</span>
                    <button data-alert-edit="1" data-alert-id="${escapeHtml(String(a.id || ""))}" style="margin-left:6px; padding:2px 8px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">✏️</button>
                    ${a.entity_type === "process_chain" ? `<button data-alert-graph="1" data-alert-id="${escapeHtml(String(a.id || ""))}" title="Посмотреть цепочку на вкладке «Графы»" style="margin-left:6px; padding:2px 8px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">🌳 цепочка</button>` : ""}
                  </td>

                </tr>
              `).join("") || `<tr><td colspan="12" class="muted">Нет данных</td></tr>`
            }
          </tbody>
        </table></div>
      </div>
    `;
  }

    function wireAlertsEditor() {
    const root = byId("anAlertsOut");
    if (!root) return;
    if (root.dataset.alertEditorBound === "1") return;
    root.dataset.alertEditorBound = "1";

    root.addEventListener("click", async (ev) => {
      const graphBtn = ev.target && ev.target.closest ? ev.target.closest('button[data-alert-graph="1"]') : null;
      if (graphBtn) {
        const alertId = graphBtn.getAttribute("data-alert-id") || "";
        if (alertId) await openGraphForAlert(alertId, LAST_ALERTS_BY_ID[alertId] || null);
        return;
      }

      const editBtn = ev.target && ev.target.closest ? ev.target.closest('button[data-alert-edit="1"]') : null;
      if (editBtn) {
        const alertId = editBtn.getAttribute("data-alert-id") || "";
        const cell = editBtn.closest('td[data-alert-status-cell="1"]');
        if (!alertId || !cell) return;

        const textEl = cell.querySelector('[data-alert-status-text="1"]');
        const current = textEl ? (textEl.textContent || "").trim().toLowerCase() : "new";

        cell.innerHTML = `
          <select data-alert-status-select="1" style="padding:2px 8px; border-radius:999px; border:1px solid #ddd; font-size:12px;">
            <option value="new">new</option>
            <option value="ack">ack</option>
            <option value="closed">closed</option>
          </select>
          <button data-alert-save="1" data-alert-id="${escapeHtml(alertId)}" style="margin-left:6px; padding:2px 8px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Сохранить</button>
          <button data-alert-cancel="1" style="margin-left:6px; padding:2px 8px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Отмена</button>
        `;

        const sel = cell.querySelector('select[data-alert-status-select="1"]');
        if (sel) sel.value = current || "new";
        return;
      }

      const saveBtn = ev.target && ev.target.closest ? ev.target.closest('button[data-alert-save="1"]') : null;
      if (saveBtn) {
        const alertId = saveBtn.getAttribute("data-alert-id") || "";
        const cell = saveBtn.closest('td[data-alert-status-cell="1"]') || saveBtn.parentElement;
        const sel = cell ? cell.querySelector('select[data-alert-status-select="1"]') : null;
        const status = sel ? sel.value : "";
        if (!alertId || !status) return;

        await apiPostJson(`/api/alerts/${encodeURIComponent(alertId)}/status`, { status });
        await refreshAnalytics();
        return;
      }

      const cancelBtn = ev.target && ev.target.closest ? ev.target.closest('button[data-alert-cancel="1"]') : null;
      if (cancelBtn) {
        await refreshAnalytics();
      }
    });
  }

  async function loadMachineProfile() {
    const host = hostSel.value || "";
    if (!host) return `<div class="card"><div class="muted">Выберите хост</div></div>`;
    const days = Number(byId("anDays")?.value || 14) || 14;
    const data = await apiGetJson(`/api/analytics/host-profile?machine_name=${encodeURIComponent(host)}&days=${encodeURIComponent(days)}&limit=100`);
    const items = data.items || [];
    return `
      <div class="card">
        <h3 style="margin-top:0;">Профиль хоста: ${escapeHtml(host)}</h3>
        <div class="tableWrap"><table>
          <thead><tr><th>Процесс</th><th>Запусков</th><th>Дней</th><th>Средняя длительность</th></tr></thead>
          <tbody>
            ${items.map(x => `<tr>
              <td><b>${escapeHtml(x.process_name || "")}</b></td>
              <td>${escapeHtml(x.runs ?? "")}</td>
              <td>${escapeHtml(x.seen_days ?? "")}</td>
              <td>${escapeHtml(fmtDuration(Number(x.avg_duration_s ?? 0)))}</td>
            </tr>`).join("") || `<tr><td colspan="4" class="muted">Нет данных</td></tr>`}
          </tbody>
        </table></div>
      </div>
    `;
  }

  async function loadProcessProfile() {
    const pname = procSel.value || "";
    if (!pname) return `<div class="card"><div class="muted">Выберите процесс</div></div>`;
    const days = Number(byId("anDays")?.value || 14) || 14;
    const data = await apiGetJson(`/api/analytics/process-profile?process_name=${encodeURIComponent(pname)}&days=${encodeURIComponent(days)}`);
    const items = data.items || [];
    return `
      <div class="card">
        <h3 style="margin-top:0;">Профиль процесса: ${escapeHtml(pname)}</h3>
        <div class="tableWrap"><table>
          <thead>
            <tr>
              <th>Машина</th><th>Пользователь</th><th>Уникальных сессий</th><th>Дней</th>
              <th>Типичные часы</th><th>Медианный RSS</th><th>Медианное CPU Δ</th><th>Медианное IO Δ</th><th>Сетевых сессий</th>
            </tr>
          </thead>
          <tbody>
            ${items.map(x => `<tr>
              <td>${escapeHtml(x.machine_name || "")}</td>
              <td>${escapeHtml(x.user_name || "")}</td>
              <td>${escapeHtml(x.runs ?? "")}</td>
              <td>${escapeHtml(x.seen_days ?? "")}</td>
              <td>${escapeHtml(x.typical_hours || "—")}</td>
              <td>${escapeHtml(fmtBytes(x.median_rss))}</td>
              <td>${escapeHtml((x.median_cpu_delta ?? "—").toString())}</td>
              <td>${escapeHtml((x.median_io_delta ?? "—").toString())}</td>
              <td>${escapeHtml(x.net_sessions ?? "")}</td>
            </tr>`).join("") || `<tr><td colspan="9" class="muted">Нет данных</td></tr>`}
          </tbody>
        </table></div>
      </div>
    `;
  }

  async function loadChainProfile() {
    const host = hostSel.value || "";
    const days = Number(byId("anDays")?.value || 14) || 14;
    const selectedChain = chainSel.value || "";

    if (!selectedChain) return `<div class="card"><div class="muted">Выберите цепочку</div></div>`;

    const q = new URLSearchParams();
    q.set("days", String(days));
    q.set("limit", "100");
    q.set("chain_key", selectedChain);
    if (host) q.set("machine_name", host);

    const data = await apiGetJson(`/api/analytics/chains?${q.toString()}`);
    const items = data.items || [];

    return `
      <div class="card">
        <h3 style="margin-top:0;">Профиль цепочки</h3>
        <div class="tableWrap"><table>
          <thead><tr><th>Машина</th><th>Корневой процесс</th><th>Цепочка</th></tr></thead>
          <tbody>
            ${items.map(x => `<tr>
              <td>${escapeHtml(x.machine_name || "")}</td>
              <td>${escapeHtml(x.root_process || "")}</td>
              <td class="mono" style="white-space:pre-wrap;">${escapeHtml(x.text || "")}</td>
            </tr>`).join("") || `<tr><td colspan="3" class="muted">Нет данных</td></tr>`}
          </tbody>
        </table></div>
      </div>
    `;
  }


  async function refreshAnalytics() {
    try {
      const alertsHtml = await loadAlerts();
      let profileHtml = "";

      if (profileTypeSel.value === "machine") profileHtml = await loadMachineProfile();
      if (profileTypeSel.value === "process") profileHtml = await loadProcessProfile();
      if (profileTypeSel.value === "chain") profileHtml = await loadChainProfile();

      alertsOut.innerHTML = alertsHtml;
      profilesOut.innerHTML = profileHtml;
      applyCollapsibleTables(alertsOut, 2);
      applyCollapsibleTables(profilesOut, 2);

    } catch (e) {
      alertsOut.innerHTML = `<div class="error">Ошибка аналитики: ${escapeHtml(e.message || String(e))}</div>`;
      profilesOut.innerHTML = "";
    }
  }

  byId("anRefresh").addEventListener("click", refreshAnalytics);
  profileTypeSel.addEventListener("change", refreshAnalytics);
  hostSel.addEventListener("change", refreshAnalytics);
  procSel.addEventListener("change", refreshAnalytics);
  chainSel.addEventListener("change", refreshAnalytics);
  if (alertMachineSel) alertMachineSel.addEventListener("change", refreshAnalytics);
  if (byId("alPeriod")) byId("alPeriod").addEventListener("change", refreshAnalytics);
  if (byId("alStatus")) byId("alStatus").addEventListener("change", refreshAnalytics);
  if (byId("alSeverity")) byId("alSeverity").addEventListener("change", refreshAnalytics);
  if (byId("alEntityType")) byId("alEntityType").addEventListener("change", refreshAnalytics);
  if (byId("alRuleType")) byId("alRuleType").addEventListener("change", refreshAnalytics);
  byId("anDays").addEventListener("change", async () => {
    await loadChainsList();
    await refreshAnalytics();
  });

  await loadHosts();
  await loadProcesses();
  await loadChainsList();
  await refreshAnalytics();
  wireAlertsEditor();
}

  // Settings tab

  async function renderSettings() {
    const root = byId("settingsRoot");
    if (!root) return;

    root.innerHTML = `
      <h2 style="margin:0 0 10px 0;">Настройки</h2>
      <div class="row">
        <div class="card" style="min-width:360px;">
          <h3 style="margin-top:0;">Машины</h3>
          <div id="setMachines"></div>
        </div>

        <div class="card" style="min-width:360px;">
          <h3 style="margin-top:0;">Роли</h3>
          <div class="badge" style="margin-bottom:10px;">
            <button id="addRoleBtn" style="padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Добавить роль</button>
          </div>
          <div id="roleAddForm" style="margin-bottom:10px;"></div>
          <div id="setRoles"></div>
        </div>
      </div>
      <div class="card" style="margin-top:12px;">
        <h3 style="margin-top:0;">Справочник процессов</h3>
        <div id="setCatalog"></div>
      </div>
      <div class="card" style="margin-top:12px;">
        <h3 style="margin-top:0;">Справочник цепочек</h3>
        <div id="setChainCatalog"></div>
      </div>

    `;

    const machinesEl = byId("setMachines");
    const rolesEl = byId("setRoles");
    const catalogEl = byId("setCatalog");
    const chainCatalogEl = byId("setChainCatalog");
    const addRoleBtn = byId("addRoleBtn");
    const roleAddForm = byId("roleAddForm");

    function parseAllowedProcessTypes(raw) {
      const out = [];
      const seen = new Set();

      for (const part of String(raw || "").split(",")) {
        const value = String(part || "").trim();
        if (!value) continue;

        const key = value.toLowerCase();
        if (seen.has(key)) continue;

        seen.add(key);
        out.push(value);
      }

      return out;
    }

    async function loadRoles() {
      const data = await apiGetJson("/api/roles");
      return data.items || [];
    }

    async function loadMachines() {
      const data = await apiGetJson("/api/machines");
      return data.items || [];
    }

    async function loadCatalog() {
      const data = await apiGetJson("/api/process-catalog");
      return data.items || [];
    }

    async function loadChainCatalog() {
      const data = await apiGetJson("/api/chain-catalog");
      return data.items || [];
    }

    async function paint() {
      try {
        const [machines, roles, catalog, chainCatalog] = await Promise.all([
          loadMachines(),
          loadRoles(),
          loadCatalog(),
          loadChainCatalog()
        ]);

        // Machines
        const rolesById = {};
        for (const r of roles) {
          const k = (r && r.role_id != null) ? String(r.role_id) : "";
          if (k) rolesById[k] = r;
        }

        const roleOptions = [`<option value="">(нет)</option>`].concat(
          roles.map(r => `<option value="${escapeHtml(String(r.role_id))}">${escapeHtml(r.role_name || "")}</option>`)
        ).join("");

        machinesEl.innerHTML = `
          <table>
            <thead><tr><th>Машина</th><th>Alias</th><th>Role</th><th>last_seen</th><th></th></tr></thead>
            <tbody>
              ${machines.map(m => {
                const name = m.machine_name || "";
                const alias = m.alias || "";
                const rid = (m.role_id != null) ? String(m.role_id) : "";
                const rname = (rid && rolesById[rid]) ? (rolesById[rid].role_name || "") : "";
                return `<tr data-mach-row="1" data-machine="${escapeHtml(name)}" data-role-id="${escapeHtml(rid)}">
                  <td><b>${escapeHtml(name)}</b></td>
                  <td class="muted" data-mach-alias-cell="1">${escapeHtml(alias)}</td>
                  <td class="muted" data-mach-role-cell="1">${escapeHtml(rname)}</td>
                  <td class="muted">${escapeHtml(fmtLocalTs(m.last_seen || ""))}</td>
                  <td><button data-mach-edit="1" style="padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">✏️</button></td>
                </tr>`;
              }).join("") || `<tr><td colspan="5" class="muted">Нет машин</td></tr>`}
            </tbody>
          </table>
        `;

        machinesEl.addEventListener("click", async (e) => {
          const t = e.target;
          if (!t || !t.matches) return;

          const row = t.closest && t.closest('tr[data-mach-row="1"]');
          if (!row) return;
          const mach = row.dataset.machine || "";

          if (t.matches('button[data-mach-edit="1"]')) {
            if (row.dataset.editing === "1") return;
            row.dataset.editing = "1";

            const aliasCell = row.querySelector('td[data-mach-alias-cell="1"]');
            const roleCell = row.querySelector('td[data-mach-role-cell="1"]');
            const oldAlias = aliasCell ? (aliasCell.textContent || "") : "";
            const oldRoleId = row.dataset.roleId || "";

            if (aliasCell) {
              aliasCell.classList.remove("muted");
              aliasCell.innerHTML = `<input data-mach-alias-input="1" type="text" value="${escapeHtml(oldAlias)}" style="width:220px; padding:4px 8px; border-radius:8px; border:1px solid #ddd; font-size:12px;" />`;
            }
            if (roleCell) {
              roleCell.classList.remove("muted");
              roleCell.innerHTML = `
                <select data-mach-role-select="1" style="padding:2px 8px; border-radius:999px; border:1px solid #ddd; font-size:12px;">
                  ${roleOptions}
                </select>
              `;
              const sel = roleCell.querySelector('select[data-mach-role-select="1"]');
              if (sel) sel.value = oldRoleId || "";
            }

            const act = row.lastElementChild;
            if (act) {
              act.innerHTML = `
                <button data-mach-save="1" style="padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Сохранить</button>
                <button data-mach-cancel="1" style="margin-left:6px; padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Отмена</button>
              `;
            }
            return;
          }

          if (t.matches('button[data-mach-cancel="1"]')) {
            await paint();
            return;
          }

          if (t.matches('button[data-mach-save="1"]')) {
            const aliasEl = row.querySelector('input[data-mach-alias-input="1"]');
            const sel = row.querySelector('select[data-mach-role-select="1"]');
            const alias = (aliasEl && aliasEl.value != null) ? String(aliasEl.value).trim() : "";
            const rid = (sel && sel.value != null) ? String(sel.value) : "";

            await apiPostJson("/api/machine-alias", { machine_name: mach, alias });
            await apiPostJson("/api/machine-role", { machine_name: mach, role_id: rid ? Number(rid) : null });

            await paint();
            return;
          }
        });

        // Roles
        rolesEl.innerHTML = `
          <table>
            <thead><tr><th>role_id</th><th>role_name</th><th>description</th><th>allowed_types</th><th></th></tr></thead>
            <tbody>
                ${roles.map(r => `<tr data-role-row="1" data-role-id="${escapeHtml(String(r.role_id ?? ""))}">
                  <td class="muted">${escapeHtml(r.role_id ?? "")}</td>
                  <td data-role-name-cell="1"><b>${escapeHtml(r.role_name || "")}</b></td>
                  <td class="muted" data-role-desc-cell="1">${escapeHtml(r.description || "")}</td>
                  <td class="mono" data-role-types-cell="1">${escapeHtml(r.allowed_types_text || "")}</td>
                  <td data-role-act-cell="1"><button data-role-edit="1" style="padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">✏️</button></td>
                </tr>`).join("") || `<tr><td colspan="5" class="muted">Нет ролей</td></tr>`}
            </tbody>
          </table>
        `;

        rolesEl.onclick = async (ev) => {
          const t = ev && ev.target;
          if (!t || !t.matches) return;

          const row = t.closest && t.closest('tr[data-role-row="1"]');
          if (!row) return;
          const rid = row.dataset.roleId || "";

          if (t.closest && t.closest('button[data-role-edit="1"]')) {
            if (row.dataset.editing === "1") return;
            row.dataset.editing = "1";

            const nameCell = row.querySelector('td[data-role-name-cell="1"]');
            const descCell = row.querySelector('td[data-role-desc-cell="1"]');
            const typesCell = row.querySelector('td[data-role-types-cell="1"]');
            const actCell = row.querySelector('td[data-role-act-cell="1"]');

            const oldName = nameCell ? (nameCell.textContent || "") : "";
            const oldDesc = descCell ? (descCell.textContent || "") : "";
            const oldTypes = typesCell ? (typesCell.textContent || "") : "";

            if (nameCell) {
              nameCell.innerHTML = `<input data-role-name-input="1" type="text" value="${escapeHtml(oldName.trim())}" style="width:240px; padding:4px 8px; border-radius:8px; border:1px solid #ddd; font-size:12px;" />`;
            }
            if (descCell) {
              descCell.classList.remove("muted");
              descCell.innerHTML = `<input data-role-desc-input="1" type="text" value="${escapeHtml(oldDesc.trim())}" style="width:420px; padding:4px 8px; border-radius:8px; border:1px solid #ddd; font-size:12px;" />`;
            }
            if (typesCell) {
              typesCell.classList.remove("mono");
              typesCell.innerHTML = `<textarea data-role-types-input="1" style="width:420px; min-height:72px; padding:4px 8px; border-radius:8px; border:1px solid #ddd; font-size:12px; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;">${escapeHtml(oldTypes.trim())}</textarea>`;
            }
            if (actCell) {
              actCell.innerHTML = `
                <button data-role-save="1" style="padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Сохранить</button>
                <button data-role-cancel="1" style="margin-left:6px; padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Отмена</button>
              `;
            }
            return;
          }

          if (t.closest && t.closest('button[data-role-cancel="1"]')) {
            await paint();
            return;
          }

          if (t.closest && t.closest('button[data-role-save="1"]')) {
            const nameEl = row.querySelector('input[data-role-name-input="1"]');
            const descEl = row.querySelector('input[data-role-desc-input="1"]');
            const typesEl = row.querySelector('textarea[data-role-types-input="1"]');
            const role_name = (nameEl && nameEl.value != null) ? String(nameEl.value).trim() : "";
            const description = (descEl && descEl.value != null) ? String(descEl.value).trim() : "";
            const allowed_process_types = parseAllowedProcessTypes((typesEl && typesEl.value != null) ? String(typesEl.value) : "");

            if (!rid) return;
            if (!role_name) return;

            await apiPostJson("/api/role", {role_id: Number(rid), role_name, description, allowed_process_types});
            await paint();
            return;
          }
        };

        // Catalog
        catalogEl.innerHTML = `
          <div class="badge" style="margin-bottom:10px; display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
            <button id="catAddBtn" style="padding:6px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">+ Добавить/обновить</button>
            <span class="muted">(ввод: process_name, type, description)</span>
          </div>
          <div id="catEditor" style="display:none; margin-bottom:10px;"></div>
          <table>
            <thead><tr><th>process_name</th><th>type</th><th>description</th><th></th></tr></thead>
            <tbody>
              ${catalog.map(c => `<tr>
                <td><b>${escapeHtml(c.process_name || "")}</b></td>
                <td class="muted">${escapeHtml(c.process_type || "")}</td>
                <td class="muted">${escapeHtml(c.description || "")}</td>
                <td><button data-cat-edit="1" data-proc="${escapeHtml(c.process_name || "")}" style="padding:2px 8px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">✏️</button></td>
              </tr>`).join("") || `<tr><td colspan="4" class="muted">Справочник пуст</td></tr>`}
            </tbody>
          </table>
        `;

        const editor = catalogEl.querySelector('#catEditor');
        const addBtn = catalogEl.querySelector('#catAddBtn');
        if (addBtn && editor) {
          addBtn.onclick = () => {
            editor.style.display = '';
            renderCatalogEditor(editor, { process_name: '', process_type: '', description: '' });
            wireCatalogEditor(editor);
          };
        }

        catalogEl.onclick = (ev) => {
          const btn = ev.target && ev.target.closest ? ev.target.closest('button[data-cat-edit="1"]') : null;
          if (!btn || !editor) return;
          const proc = btn.dataset.proc || '';
          const row = (catalog || []).find(x => String(x.process_name || '') === proc) || { process_name: proc, process_type: '', description: '' };
          editor.style.display = '';
          renderCatalogEditor(editor, row);
          wireCatalogEditor(editor);
        };

        function wireCatalogEditor(ed) {
          const save = ed.querySelector('#catSave');
          const cancel = ed.querySelector('#catCancel');
          const pn = ed.querySelector('#catProcName');
          const pt = ed.querySelector('#catProcType');
          const pd = ed.querySelector('#catProcDesc');
          if (cancel) cancel.onclick = () => { ed.style.display = 'none'; ed.innerHTML = ''; };
          if (save) save.onclick = async () => {
            const process_name = (pn && pn.value ? pn.value : '').trim();
            const process_type = (pt && pt.value ? pt.value : '').trim();
            const description = (pd && pd.value ? pd.value : '').trim();
            if (!process_name) return;
            await apiPostJson('/api/process-catalog-item', { process_name, process_type, description });
            await paint();
          };
        }

                chainCatalogEl.innerHTML = `
          <div class="badge" style="margin-bottom:10px; display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
            <button id="chainCatAddBtn" style="padding:6px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">+ Добавить/обновить</button>
            <span class="muted">(ввод: chain_name, chain_key, type, description)</span>
          </div>
          <div id="chainCatEditor" style="display:none; margin-bottom:10px;"></div>
          <table>
            <thead><tr><th>chain_name</th><th>chain_key</th><th>type</th><th>description</th><th></th></tr></thead>
            <tbody>
              ${chainCatalog.map(c => `<tr>
                <td><b>${escapeHtml(c.chain_name || "")}</b></td>
                <td class="mono">${escapeHtml(c.chain_key || "")}</td>
                <td class="muted">${escapeHtml(c.chain_type || "")}</td>
                <td class="muted">${escapeHtml(c.description || "")}</td>
                <td><button data-chain-cat-edit="1" data-chain-key="${escapeHtml(c.chain_key || "")}" style="padding:2px 8px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">✏️</button></td>
              </tr>`).join("") || `<tr><td colspan="5" class="muted">Справочник цепочек пуст</td></tr>`}
            </tbody>
          </table>
        `;

        const chainEditor = chainCatalogEl.querySelector('#chainCatEditor');
        const chainAddBtn = chainCatalogEl.querySelector('#chainCatAddBtn');

        if (chainAddBtn && chainEditor) {
          chainAddBtn.onclick = () => {
            chainEditor.style.display = '';
            renderChainCatalogEditor(chainEditor, { chain_name: '', chain_key: '', chain_type: '', description: '' });
            wireChainCatalogEditor(chainEditor);
          };
        }

        chainCatalogEl.onclick = (ev) => {
          const btn = ev.target && ev.target.closest ? ev.target.closest('button[data-chain-cat-edit="1"]') : null;
          if (!btn || !chainEditor) return;
          const chainKey = btn.dataset.chainKey || '';
          const row = (chainCatalog || []).find(x => String(x.chain_key || '') === chainKey) || { chain_name: '', chain_key: chainKey, chain_type: '', description: '' };
          chainEditor.style.display = '';
          renderChainCatalogEditor(chainEditor, row);
          wireChainCatalogEditor(chainEditor);
        };

        function wireChainCatalogEditor(ed) {
          const save = ed.querySelector('#chainCatSave');
          const cancel = ed.querySelector('#chainCatCancel');
          const nameEl = ed.querySelector('#chainCatName');
          const keyEl = ed.querySelector('#chainCatKey');
          const typeEl = ed.querySelector('#chainCatType');
          const descEl = ed.querySelector('#chainCatDesc');

          if (cancel) cancel.onclick = () => { ed.style.display = 'none'; ed.innerHTML = ''; };
          if (save) save.onclick = async () => {
            const chain_name = (nameEl && nameEl.value ? nameEl.value : '').trim();
            const chain_key = (keyEl && keyEl.value ? keyEl.value : '').trim();
            const chain_type = (typeEl && typeEl.value ? typeEl.value : '').trim();
            const description = (descEl && descEl.value ? descEl.value : '').trim();

            if (!chain_name || !chain_key) return;
            await apiPostJson('/api/chain-catalog-item', { chain_name, chain_key, chain_type, description });
            await paint();
          };
        }
        applyCollapsibleTables(machinesEl, 2);
        applyCollapsibleTables(rolesEl, 2);
        applyCollapsibleTables(catalogEl, 2);
        applyCollapsibleTables(chainCatalogEl, 2);

      } catch (e) {
        root.insertAdjacentHTML("beforeend", `<div class="error">Ошибка настроек: ${escapeHtml(e.message || String(e))}</div>`);
      }
    }

    addRoleBtn.addEventListener("click", async () => {
      // Add role flow like process catalog: open form -> fill fields -> save
      if (!roleAddForm) return;
      if (roleAddForm.querySelector('input[data-role-add-name="1"]')) return;

      roleAddForm.innerHTML = `
        <div class="badge" style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
          <input data-role-add-name="1" type="text" placeholder="role_name (например: бухгалтерия)" style="width:240px; padding:4px 8px; border-radius:8px; border:1px solid #ddd; font-size:12px;" />
          <input data-role-add-desc="1" type="text" placeholder="description (например: ПК в бухгалтерии)" style="width:360px; padding:4px 8px; border-radius:8px; border:1px solid #ddd; font-size:12px;" />
          <input data-role-add-types="1" type="text" placeholder="allowed types (например: browser, utility, office)" style="width:360px; padding:4px 8px; border-radius:8px; border:1px solid #ddd; font-size:12px;" />
          <button data-role-add-save="1" style="padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Сохранить</button>
          <button data-role-add-cancel="1" style="padding:2px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Отмена</button>
        </div>
      `;

      const onClick = async (e) => {
        const t = e && e.target;
        if (!t || !t.matches) return;
        if (t.matches('button[data-role-add-cancel="1"]')) {
          roleAddForm.innerHTML = "";
          roleAddForm.removeEventListener("click", onClick);
          return;
        }
        if (t.matches('button[data-role-add-save="1"]')) {
          const nameEl = roleAddForm.querySelector('input[data-role-add-name="1"]');
          const descEl = roleAddForm.querySelector('input[data-role-add-desc="1"]');
          const typesEl = roleAddForm.querySelector('input[data-role-add-types="1"]');
          const role_name = (nameEl && nameEl.value ? nameEl.value : "").trim();
          const description = (descEl && descEl.value ? descEl.value : "").trim();
          const allowed_process_types = parseAllowedProcessTypes(typesEl && typesEl.value ? typesEl.value : "");
          if (!role_name) return;
          await apiPostJson("/api/roles", { role_name, description, allowed_process_types });
          roleAddForm.innerHTML = "";
          roleAddForm.removeEventListener("click", onClick);
          await paint();
        }
      };
      roleAddForm.addEventListener("click", onClick);
    });
;

    await paint();
  }

  function renderCatalogEditor(container, initial) {
    const val = initial || { process_name: "", process_type: "", description: "" };
    container.innerHTML = `
      <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
        <input id="catProcName" type="text" placeholder="process_name (например: chrome.exe)" value="${escapeHtml(val.process_name || "")}" style="width:220px; padding:6px 10px; border-radius:999px; border:1px solid #ddd;" />
        <input id="catProcType" type="text" placeholder="type (например: browser)" value="${escapeHtml(val.process_type || "")}" style="width:180px; padding:6px 10px; border-radius:999px; border:1px solid #ddd;" />
        <input id="catProcDesc" type="text" placeholder="description" value="${escapeHtml(val.description || "")}" style="flex:1; min-width:260px; padding:6px 10px; border-radius:999px; border:1px solid #ddd;" />
        <button id="catSave" style="padding:6px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Сохранить</button>
        <button id="catCancel" style="padding:6px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Отмена</button>
      </div>
    `;
  }

  function renderChainCatalogEditor(container, initial) {
    const val = initial || { chain_name: "", chain_key: "", chain_type: "", description: "" };
    container.innerHTML = `
      <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
        <input id="chainCatName" type="text" placeholder="chain_name" value="${escapeHtml(val.chain_name || "")}" style="width:220px; padding:6px 10px; border-radius:999px; border:1px solid #ddd;" />
        <input id="chainCatKey" type="text" placeholder="chain_key" value="${escapeHtml(val.chain_key || "")}" style="width:360px; padding:6px 10px; border-radius:999px; border:1px solid #ddd;" />
        <input id="chainCatType" type="text" placeholder="type" value="${escapeHtml(val.chain_type || "")}" style="width:180px; padding:6px 10px; border-radius:999px; border:1px solid #ddd;" />
        <input id="chainCatDesc" type="text" placeholder="description" value="${escapeHtml(val.description || "")}" style="flex:1; min-width:260px; padding:6px 10px; border-radius:999px; border:1px solid #ddd;" />
        <button id="chainCatSave" style="padding:6px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Сохранить</button>
        <button id="chainCatCancel" style="padding:6px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Отмена</button>
      </div>
    `;
  }

  // ---------------------------------------------------------------------------
  // Graphs tab (эпик 3, фазы B/C): дерево процессов от алерта или от текущего
  // процесса поверх GET /api/analytics/process-tree. Раскладка — по уровням
  // сверху вниз (предки над корнем, потомки под ним), карточки — div с
  // absolute-позиционированием, рёбра — один SVG-слой, pan/zoom — CSS transform.
  // ---------------------------------------------------------------------------

  const GRAPH = {
    lastPreset: null,     // чтобы повторное открытие вкладки восстанавливало выбор
    alertsById: {},       // алерты из списка «Из алерта» (для подсветки без лишних запросов)
    tree: null,
    layout: null,
    panzoom: null,
  };

  const GR_NODE_W = 200, GR_NODE_H = 62, GR_GAP_X = 26, GR_GAP_Y = 54;

  function grAlertChainLabel(a) {
    const proc = a.process_name || "";
    if (a.parent_process_name) return `${a.parent_process_name} -> ${proc}`;
    if ((a.metric || "") === "chain_fanout_anomaly") return `${proc} -> *`;
    return proc;
  }

  function grNodeId(pid, startTime) {
    return `${Number(pid) || 0}|${startTime || ""}`;
  }

  // Раскладка: потомки — по количеству листьев в поддереве (родитель по центру над
  // детьми, порядок детей как пришёл с сервера), предки — цепочка над корнем.
  function grLayout(tree) {
    const byId = {};
    for (const n of (tree.nodes || [])) byId[n.node_id] = n;
    const rootId = tree.root.node_id;

    const cx = {};
    let cursor = 0;
    function place(id) {
      const n = byId[id];
      const kids = (n.children_ids || []).filter(k => byId[k] && byId[k].level > n.level);
      if (!kids.length) { cx[id] = cursor + 0.5; cursor += 1; return cx[id]; }
      const xs = kids.map(place);
      cx[id] = (xs[0] + xs[xs.length - 1]) / 2;
      return cx[id];
    }
    place(rootId);
    for (const aid of (tree.ancestor_ids || [])) cx[aid] = cx[rootId];

    const levels = (tree.nodes || []).map(n => n.level);
    const minLevel = Math.min(0, ...levels);
    const maxLevel = Math.max(0, ...levels);
    // сверху резервируем ряд под пометку «предки обрезаны», снизу — под «потомки обрезаны»
    const topRows = tree.ancestors_truncated ? 1 : 0;

    const pos = {};
    for (const n of (tree.nodes || [])) {
      if (cx[n.node_id] == null) continue;   // узел вне пути/поддерева (не должно случаться)
      pos[n.node_id] = {
        x: cx[n.node_id] * (GR_NODE_W + GR_GAP_X) - GR_NODE_W / 2 + GR_GAP_X,
        y: (n.level - minLevel + topRows) * (GR_NODE_H + GR_GAP_Y) + GR_GAP_Y / 2,
      };
    }
    const rows = (maxLevel - minLevel + 1) + topRows + (tree.descendants_truncated ? 1 : 0);
    return {
      byId, pos, rootId, minLevel, maxLevel, topRows,
      width: cursor * (GR_NODE_W + GR_GAP_X) + GR_GAP_X,
      height: rows * (GR_NODE_H + GR_GAP_Y) + GR_GAP_Y,
    };
  }

  function grRenderTree(canvas, tree, layout) {
    canvas.innerHTML = "";
    canvas.style.width = `${layout.width}px`;
    canvas.style.height = `${layout.height}px`;

    // рёбра
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("class", "grEdges");
    svg.setAttribute("width", String(layout.width));
    svg.setAttribute("height", String(layout.height));
    for (const n of (tree.nodes || [])) {
      const p = n.parent_id ? layout.pos[n.parent_id] : null;
      const c = layout.pos[n.node_id];
      if (!p || !c) continue;
      const line = document.createElementNS(svgNS, "line");
      line.setAttribute("x1", String(p.x + GR_NODE_W / 2));
      line.setAttribute("y1", String(p.y + GR_NODE_H));
      line.setAttribute("x2", String(c.x + GR_NODE_W / 2));
      line.setAttribute("y2", String(c.y));
      svg.appendChild(line);
    }
    canvas.appendChild(svg);

    // карточки
    for (const n of (tree.nodes || [])) {
      const p = layout.pos[n.node_id];
      if (!p) continue;
      const el = document.createElement("div");
      el.className = "grNode" + (n.is_root ? " grRoot" : "");
      el.dataset.nodeId = n.node_id;
      el.style.left = `${p.x}px`;
      el.style.top = `${p.y}px`;
      el.style.width = `${GR_NODE_W}px`;
      el.style.height = `${GR_NODE_H}px`;
      const user = normUsername(n.user_name || "");
      const lvl = n.level === 0 ? "корень" : (n.level < 0 ? `предок ${-n.level}` : `уровень ${n.level}`);
      el.title = `${n.process_name || "unknown"}\npid ${n.pid ?? "—"}, ppid ${n.ppid ?? "—"}\n${lvl}\nстарт: ${fmtLocalTs(n.start_time || "")}` +
        (n.end_time ? `\nконец: ${fmtLocalTs(n.end_time)}` : "") + (n.exe_path ? `\n${n.exe_path}` : "");
      el.innerHTML = `
        <div class="grName">${escapeHtml(n.process_name || "unknown")}</div>
        <div class="grMeta">pid ${escapeHtml(n.pid ?? "—")}${user ? " · " + escapeHtml(user) : ""}</div>
        <div class="grMeta">${escapeHtml(fmtLocalTs(n.start_time || ""))}${n.end_time ? " → " + escapeHtml(fmtLocalTs(n.end_time)) : ""}</div>`;
      canvas.appendChild(el);
    }

    // пометки об обрезке по границам дерева
    const maxDepth = tree.max_depth;
    if (tree.ancestors_truncated) {
      const top = layout.pos[(tree.ancestor_ids || [])[tree.ancestor_ids.length - 1]] || layout.pos[layout.rootId];
      const m = document.createElement("div");
      m.className = "grTrunc";
      m.style.left = `${top.x}px`;
      m.style.top = `${top.y - GR_NODE_H - GR_GAP_Y}px`;
      m.style.width = `${GR_NODE_W}px`;
      m.textContent = `⋯ дальше не показано: глубина предков превышает лимит (${maxDepth})`;
      canvas.appendChild(m);
    }
    if (tree.descendants_truncated) {
      const m = document.createElement("div");
      m.className = "grTrunc";
      m.style.left = `${GR_GAP_X}px`;
      m.style.top = `${(layout.maxLevel - layout.minLevel + layout.topRows + 1) * (GR_NODE_H + GR_GAP_Y) + GR_GAP_Y / 2}px`;
      m.style.width = `${Math.max(GR_NODE_W, layout.width - 2 * GR_GAP_X)}px`;
      m.textContent = `⋯ дальше не показано: глубина потомков превышает лимит (${maxDepth} уровней)`;
      canvas.appendChild(m);
    }
  }

  // Какие узлы подсвечивать для алерта: якорь = сессия из самого алерта;
  // chain_fanout_anomaly (привязан к родителю) — плюс все его дети («веер»);
  // chain_depth_anomaly — плюс весь путь предков (аномалия в длине цепочки);
  // прочие process_chain (chain_rarity) — плюс прямой родитель (ребро parent -> child).
  function grHighlightSetForAlert(alert, layout) {
    const anchor = grNodeId(alert.pid, alert.start_time);
    const ids = new Set();
    const a = layout.byId[anchor];
    if (!a) return { anchor, ids };
    ids.add(anchor);
    const m = alert.metric || "";
    if (m === "chain_fanout_anomaly") {
      for (const c of (a.children_ids || [])) if (layout.byId[c]) ids.add(c);
    } else if (m === "chain_depth_anomaly") {
      let p = a.parent_id;
      while (p && layout.byId[p]) { ids.add(p); p = layout.byId[p].parent_id; }
    } else if (alert.entity_type === "process_chain" && a.parent_id && layout.byId[a.parent_id]) {
      ids.add(a.parent_id);
    }
    return { anchor, ids };
  }

  function grApplyAlerts(canvas, layout, alerts) {
    canvas.querySelectorAll(".grNode.grAlert").forEach(el => el.classList.remove("grAlert"));
    canvas.querySelectorAll(".grLabel").forEach(el => el.remove());
    const labelsByAnchor = {};
    for (const al of (alerts || [])) {
      const { anchor, ids } = grHighlightSetForAlert(al, layout);
      for (const id of ids) {
        const el = canvas.querySelector(`.grNode[data-node-id="${CSS.escape(id)}"]`);
        if (el) el.classList.add("grAlert");
      }
      if (layout.byId[anchor]) (labelsByAnchor[anchor] = labelsByAnchor[anchor] || []).push(al);
    }
    for (const anchor of Object.keys(labelsByAnchor)) {
      const p = layout.pos[anchor];
      if (!p) continue;
      const lbl = document.createElement("div");
      lbl.className = "grLabel";
      lbl.style.left = `${p.x + GR_NODE_W + 8}px`;
      lbl.style.top = `${p.y}px`;
      lbl.textContent = labelsByAnchor[anchor].map(al => `[${al.severity || ""}] ${al.reason || al.metric || ""}`).join("\n");
      canvas.appendChild(lbl);
    }
  }

  function grSoftNote(canvas, layout, nodeId, text) {
    canvas.querySelectorAll(".grLabel.soft").forEach(el => el.remove());
    const p = layout.pos[nodeId];
    if (!p) return;
    const lbl = document.createElement("div");
    lbl.className = "grLabel soft";
    lbl.style.left = `${p.x + GR_NODE_W + 8}px`;
    lbl.style.top = `${p.y}px`;
    lbl.textContent = text;
    canvas.appendChild(lbl);
  }

  // Pan/zoom на CSS transform: drag мышью, масштаб колесом вокруг курсора.
  function grWirePanZoom(viewport, canvas) {
    const st = { scale: 1, tx: 0, ty: 0, dragging: false, sx: 0, sy: 0, moved: 0 };
    const apply = () => { canvas.style.transform = `translate(${st.tx}px, ${st.ty}px) scale(${st.scale})`; };
    viewport.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = viewport.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      const ns = Math.min(3, Math.max(0.15, st.scale * f));
      st.tx = mx - (mx - st.tx) * (ns / st.scale);
      st.ty = my - (my - st.ty) * (ns / st.scale);
      st.scale = ns;
      apply();
    }, { passive: false });
    viewport.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      st.dragging = true; st.moved = 0;
      st.sx = e.clientX - st.tx; st.sy = e.clientY - st.ty;
      viewport.classList.add("dragging");
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!st.dragging) return;
      const nx = e.clientX - st.sx, ny = e.clientY - st.sy;
      st.moved += Math.abs(nx - st.tx) + Math.abs(ny - st.ty);
      st.tx = nx; st.ty = ny;
      apply();
    });
    window.addEventListener("mouseup", () => { st.dragging = false; viewport.classList.remove("dragging"); });
    return {
      state: st,
      set(tx, ty, scale) { st.tx = tx; st.ty = ty; st.scale = scale; apply(); },
      wasDrag() { return st.moved > 4; },
    };
  }

  async function renderGraphs(preset) {
    const root = byId("graphsRoot");
    if (!root) return;
    if (!preset) preset = GRAPH.lastPreset;
    GRAPH.lastPreset = preset || null;

    root.innerHTML = `
      <h2 style="margin:0 0 10px 0;">Графы цепочек процессов</h2>
      <div class="card" style="margin-bottom:12px;">
        <div class="badge" style="display:flex; flex-wrap:wrap; gap:10px; align-items:center;">
          <span class="muted">Источник корня:</span>
          <label class="chk"><input type="radio" name="grMode" value="alert" checked> Из алерта</label>
          <label class="chk"><input type="radio" name="grMode" value="process"> Текущий процесс</label>
          <span id="grModeAlert" style="display:inline-flex; gap:8px; align-items:center;">
            <select id="grAlertSel" style="max-width:640px;"><option value="">(загрузка алертов...)</option></select>
            <button id="grAlertsReload" style="padding:6px 10px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Обновить список</button>
          </span>
          <span id="grModeProc" style="display:none; gap:8px; align-items:center;">
            <label class="muted">Машина:</label>
            <select id="grMachineSel"><option value="">(загрузка...)</option></select>
            <label class="muted">Процесс:</label>
            <select id="grProcSel" style="max-width:520px;"><option value="">(выберите машину)</option></select>
          </span>
          <span id="grStatus" class="muted"></span>
        </div>
        <div class="grLegend">
          <span>Предки — сверху, потомки — снизу, корень выделен жирной рамкой.</span>
          <span>Перетаскивание — мышью, масштаб — колесом.</span>
          <span>Клик по узлу — проверить, есть ли по нему алерты.</span>
          <button id="grFit" style="padding:2px 8px; border-radius:999px; border:1px solid #ddd; cursor:pointer;">Сбросить вид</button>
        </div>
        <div id="grInfo" class="muted" style="margin:4px 0 8px 0; white-space:pre-wrap;"></div>
        <div id="grViewport" class="grViewport"><div id="grCanvas" class="grCanvas"></div></div>
      </div>
    `;

    const status = byId("grStatus");
    const info = byId("grInfo");
    const viewport = byId("grViewport");
    const canvas = byId("grCanvas");
    const alertSel = byId("grAlertSel");
    const machineSel = byId("grMachineSel");
    const procSel = byId("grProcSel");
    const modeAlert = byId("grModeAlert");
    const modeProc = byId("grModeProc");

    GRAPH.panzoom = grWirePanZoom(viewport, canvas);
    GRAPH.tree = null;
    GRAPH.layout = null;

    let latestCache = null;   // ответ /api/latest для режима «Текущий процесс»

    function currentMode() {
      const r = root.querySelector('input[name="grMode"]:checked');
      return r ? r.value : "alert";
    }

    function showMode(mode) {
      modeAlert.style.display = mode === "alert" ? "inline-flex" : "none";
      modeProc.style.display = mode === "process" ? "inline-flex" : "none";
    }

    function fitView() {
      if (!GRAPH.layout) return;
      const rp = GRAPH.layout.pos[GRAPH.layout.rootId];
      const vw = viewport.clientWidth || 800;
      const scale = 1;
      const tx = Math.round(vw / 2 - (rp.x + GR_NODE_W / 2) * scale);
      GRAPH.panzoom.set(tx, 16, scale);
    }

    function describeTree(tree) {
      const n = (tree.nodes || []).length;
      const anc = (tree.ancestor_ids || []).length;
      const desc = n - anc - 1;
      const parts = [`Машина ${tree.root.machine_name}: корень pid ${tree.root.pid} (старт ${fmtLocalTs(tree.root.start_time)}), предков: ${anc}, потомков: ${desc}, лимит глубины: ${tree.max_depth}.`];
      if (tree.ancestors_truncated) parts.push("⚠ Путь предков обрезан: дальше не показано, глубина превышает лимит.");
      if (tree.descendants_truncated) parts.push("⚠ Поддерево потомков обрезано: дальше не показано, глубина превышает лимит.");
      return parts.join("\n");
    }

    async function buildTree(params, alertsToHighlight, note) {
      status.textContent = "Строю дерево...";
      info.textContent = "";
      canvas.innerHTML = "";
      try {
        const q = new URLSearchParams();
        for (const k of Object.keys(params)) q.set(k, String(params[k]));
        const tree = await apiGetJson(`/api/analytics/process-tree?${q.toString()}`);
        GRAPH.tree = tree;
        GRAPH.layout = grLayout(tree);
        grRenderTree(canvas, tree, GRAPH.layout);
        fitView();
        let text = describeTree(tree);
        if (alertsToHighlight && alertsToHighlight.length) {
          grApplyAlerts(canvas, GRAPH.layout, alertsToHighlight);
          text += "\n" + alertsToHighlight.map(a => `Алерт #${a.id} (${a.metric}, ${a.severity}): ${a.reason || ""}`).join("\n");
        }
        if (note) text += "\n" + note;
        info.textContent = text;
        status.textContent = "";
      } catch (e) {
        status.textContent = "";
        info.innerHTML = `<span class="error">Ошибка построения дерева: ${escapeHtml(e.message || String(e))}</span>`;
      }
    }

    async function loadAlertsList(selectedId) {
      const data = await apiGetJson("/api/alerts?entity_type=process_chain&limit=50&offset=0");
      const items = data.items || [];
      GRAPH.alertsById = {};
      for (const a of items) GRAPH.alertsById[String(a.id)] = a;
      alertSel.innerHTML = `<option value="">(выберите алерт по цепочке)</option>` + items.map(a => {
        const label = `${fmtLocalTs(a.created_at || a.sample_time || "")} · ${a.machine_name || ""} · ${grAlertChainLabel(a)} · ${a.severity || ""}`;
        return `<option value="${escapeHtml(String(a.id))}">${escapeHtml(label)}</option>`;
      }).join("");
      if (selectedId != null && !GRAPH.alertsById[String(selectedId)]) {
        // алерт пришёл из таблицы аналитики, но не попал в последние 50 — добавляем отдельной опцией
        const a = (preset && preset.alert) ? preset.alert : null;
        const label = a ? `${fmtLocalTs(a.created_at || "")} · ${a.machine_name || ""} · ${grAlertChainLabel(a)} · ${a.severity || ""}` : `алерт #${selectedId}`;
        alertSel.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(String(selectedId))}">${escapeHtml(label)}</option>`);
        if (a) GRAPH.alertsById[String(selectedId)] = a;
      }
      if (selectedId != null) alertSel.value = String(selectedId);
    }

    async function buildFromAlert(alertId) {
      const a = GRAPH.alertsById[String(alertId)] || null;
      await buildTree({ alert_id: alertId }, a ? [a] : []);
    }

    async function loadMachines(selected) {
      const m = await apiGetJson("/api/machines");
      const items = (m && m.items) ? m.items : [];
      machineSel.innerHTML = `<option value="">(выберите машину)</option>` + items.map(x => {
        const name = x.machine_name || "";
        const alias = x.alias ? ` (${x.alias})` : "";
        return `<option value="${escapeHtml(name)}">${escapeHtml(name + alias)}</option>`;
      }).join("");
      if (selected) machineSel.value = selected;
    }

    async function loadProcesses(machine, selectedValue) {
      procSel.innerHTML = `<option value="">(загрузка...)</option>`;
      if (!latestCache) latestCache = await apiGetJson("/api/latest");
      const mrow = (latestCache.latest || []).find(x => (x.machine_name || "") === machine);
      const running = (mrow && mrow.running_main) ? mrow.running_main.slice() : [];
      running.sort((a, b) => norm(a.process_name).localeCompare(norm(b.process_name), "ru") || (Number(a.pid) || 0) - (Number(b.pid) || 0));
      procSel.innerHTML = `<option value="">(выберите процесс: запущенных ${running.length})</option>` + running.map(ev => {
        const val = grNodeId(ev.pid, ev.start_time);
        const user = normUsername(ev.user_name || "");
        const label = `${ev.process_name || "unknown"} (pid ${ev.pid ?? "—"}${user ? ", " + user : ""}, с ${fmtLocalTs(ev.start_time || "")})`;
        return `<option value="${escapeHtml(val)}">${escapeHtml(label)}</option>`;
      }).join("");
      if (selectedValue) {
        if (!running.some(ev => grNodeId(ev.pid, ev.start_time) === selectedValue)) {
          // процесс из карточки уже не в «запущенных» — всё равно даём построить дерево по его координатам
          procSel.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(selectedValue)}">${escapeHtml("pid " + selectedValue.replace("|", ", старт "))} (не в текущем срезе)</option>`);
        }
        procSel.value = selectedValue;
      }
    }

    async function buildFromProcess() {
      const machine = machineSel.value || "";
      const val = procSel.value || "";
      if (!machine || !val) return;
      const i = val.indexOf("|");
      const pid = val.slice(0, i), start = val.slice(i + 1);
      await buildTree({ machine_name: machine, pid, start_time: start }, [],
        "Автоподсветки нет: кликните по узлу, чтобы проверить алерты по нему.");
    }

    // клик по узлу: точечный запрос «есть ли алерт по этой сессии»
    canvas.addEventListener("click", async (e) => {
      if (GRAPH.panzoom.wasDrag()) return;
      const el = e.target && e.target.closest ? e.target.closest(".grNode") : null;
      if (!el || !GRAPH.tree || !GRAPH.layout) return;
      const nodeId = el.dataset.nodeId || "";
      const n = GRAPH.layout.byId[nodeId];
      if (!n) return;
      canvas.querySelectorAll(".grNode.grSelected").forEach(x => x.classList.remove("grSelected"));
      el.classList.add("grSelected");
      status.textContent = "Проверяю алерты по узлу...";
      try {
        const q = new URLSearchParams();
        q.set("machine", GRAPH.tree.root.machine_name);
        q.set("pid", String(n.pid ?? 0));
        q.set("start_time", n.start_time || "");
        q.set("limit", "50");
        const data = await apiGetJson(`/api/alerts?${q.toString()}`);
        const items = data.items || [];
        status.textContent = "";
        const head = `Узел ${n.process_name || "unknown"} (pid ${n.pid ?? "—"}, старт ${fmtLocalTs(n.start_time || "")}): `;
        if (items.length) {
          grApplyAlerts(canvas, GRAPH.layout, items);
          info.textContent = describeTree(GRAPH.tree) + "\n" + head + `алертов: ${items.length}\n` +
            items.map(a => `• #${a.id} ${a.metric} [${a.severity}] ${fmtLocalTs(a.created_at || "")}: ${a.reason || ""}`).join("\n");
        } else {
          grSoftNote(canvas, GRAPH.layout, nodeId, "алертов по этому узлу нет");
          info.textContent = describeTree(GRAPH.tree) + "\n" + head + "алертов по этому узлу нет.";
        }
      } catch (err) {
        status.textContent = "";
        info.innerHTML = `<span class="error">Ошибка запроса алертов: ${escapeHtml(err.message || String(err))}</span>`;
      }
    });

    root.querySelectorAll('input[name="grMode"]').forEach(r => r.addEventListener("change", async () => {
      const mode = currentMode();
      showMode(mode);
      canvas.innerHTML = "";
      info.textContent = "";
      GRAPH.tree = null; GRAPH.layout = null;
      try {
        // списки грузим лениво при первом переключении в режим
        if (mode === "process" && machineSel.options.length <= 1) await loadMachines("");
        if (mode === "alert" && alertSel.options.length <= 1) await loadAlertsList(null);
      } catch (e) {
        info.innerHTML = `<span class="error">${escapeHtml(e.message || String(e))}</span>`;
      }
    }));
    alertSel.addEventListener("change", () => { if (alertSel.value) buildFromAlert(alertSel.value); });
    byId("grAlertsReload").addEventListener("click", async () => {
      try { await loadAlertsList(alertSel.value || null); }
      catch (e) { info.innerHTML = `<span class="error">${escapeHtml(e.message || String(e))}</span>`; }
    });
    machineSel.addEventListener("change", async () => {
      if (!machineSel.value) { procSel.innerHTML = `<option value="">(выберите машину)</option>`; return; }
      try { await loadProcesses(machineSel.value); }
      catch (e) { info.innerHTML = `<span class="error">${escapeHtml(e.message || String(e))}</span>`; }
    });
    procSel.addEventListener("change", buildFromProcess);
    byId("grFit").addEventListener("click", fitView);

    // стартовое состояние (в т.ч. переход с других вкладок)
    try {
      const mode = (preset && preset.mode) ? preset.mode : "alert";
      const r = root.querySelector(`input[name="grMode"][value="${mode}"]`);
      if (r) r.checked = true;
      showMode(mode);
      if (mode === "alert") {
        await loadAlertsList(preset && preset.alertId != null ? preset.alertId : null);
        if (preset && preset.alertId != null) await buildFromAlert(preset.alertId);
      } else {
        await loadMachines(preset ? preset.machine : "");
        if (preset && preset.machine) {
          await loadProcesses(preset.machine, preset.pid != null ? grNodeId(preset.pid, preset.start_time) : "");
          if (preset.pid != null) await buildFromProcess();
        }
      }
    } catch (e) {
      info.innerHTML = `<span class="error">${escapeHtml(e.message || String(e))}</span>`;
    }
  }

  // Точки входа с других вкладок (фаза C)
  async function openGraphForAlert(alertId, alertObj) {
    setActiveTab("graphs");
    await renderGraphs({ mode: "alert", alertId, alert: alertObj || null });
  }

  async function openGraphForProcess(machineName, pid, startTime) {
    setActiveTab("graphs");
    await renderGraphs({ mode: "process", machine: machineName, pid, start_time: startTime });
  }

  // prevent auto-refresh from spamming while not on Monitoring (safe)
  (function hookTabs() {
    if (typeof fetchLatest === "function") {
      const _orig = fetchLatest;
      window.fetchLatest = async function() {
        if (getActiveTab() !== "monitoring") return;
        return await _orig();
      };
    }
  })();

  // export to global so existing HTML hooks keep working
  window.fetchLatest = fetchLatest;
  window.renderAnalytics = renderAnalytics;
  window.renderSettings = renderSettings;
  window.renderGraphs = renderGraphs;
  window.openGraphForAlert = openGraphForAlert;
  window.openGraphForProcess = openGraphForProcess;
})();

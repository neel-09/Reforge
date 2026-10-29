/* ==========================================================================
   Reforge — frontend app (vanilla JS, no build step)
   Landing (composer) → Execution trace (streamed) → Results (chat + outputs)
   ========================================================================== */
(() => {
  "use strict";

  const CFG = Object.assign({ API_BASE: "", DEMO: "auto" }, window.REFORGE_CONFIG || {});
  const API = (CFG.API_BASE || "").replace(/\/$/, "");
  const params = new URLSearchParams(location.search);
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const state = { mode: "demo", running: false, input: null, result: null, activeComposer: null };

  /* ---------------------------------------------------------------- utils */
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const usd = (x, d = 4) => (x == null || isNaN(+x) ? "—" : "$" + (+x).toFixed(d));
  const pct = (x) => (x == null || isNaN(+x) ? "—" : `${Math.round(+x)}%`);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const base = (p) => String(p).split("/").pop();
  const ext = (p) => (String(p).match(/\.([a-z0-9]+)$/i) || [, ""])[1].toLowerCase();
  const fmtClock = (s) => { const m = Math.floor(s / 60); return `${String(m).padStart(2, "0")}:${(s - m * 60).toFixed(1).padStart(4, "0")}`; };
  const abs = (u) => (/^https?:/.test(u) ? u : API + u);

  let toastT;
  function toast(msg, err = false) {
    const t = $("#toast");
    t.textContent = msg; t.classList.toggle("err", err); t.classList.add("on");
    clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("on"), 4200);
  }

  /* ---------------------------------------------------------------- stages */
  const STAGES = [
    { id: "ingest", name: "Ingest", short: "Ingest", module: "endpoints · normalise", est: 4,
      desc: "Normalise the input, then clone the repository or stage the snippet as a testable workspace.",
      subs: ["Normalising input", "Syncing workspace", "Generating test harness hooks"] },
    { id: "scan", name: "Scan & baseline", short: "Scan", module: "extract/scanner · harness", est: 10,
      desc: "Detect the pinned dependency, look up known CVEs on OSV, and record the v1 test baseline.",
      subs: ["Detecting pydantic version", "Checking OSV advisories", "Running v1 baseline tests"] },
    { id: "break", name: "Break & slice", short: "Break", module: "sandbox/sandbox · slicer", est: 12,
      desc: "Install the new major version in an isolated sandbox, watch what breaks, and slice the code with a static AST scan.",
      subs: ["Installing pydantic v2 in sandbox", "Running tests under v2", "AST scan · import-graph ordering"] },
    { id: "plan", name: "Ground & plan", short: "Plan", module: "planner/grounder · planner", est: 14,
      desc: "Ground every breaking symbol in the official migration docs, then let Nemotron 3 Ultra write a validated refactor plan.",
      subs: ["Fetching official migration docs", "Nemotron 3 Ultra drafting the plan", "Guardrails · plan validation"] },
    { id: "patch", name: "Patch & self-heal", short: "Patch", module: "route_patch/switchyard · patcher", est: 18,
      desc: "Apply SEARCH/REPLACE hunks step by step — Lightning first, escalating to Super and Ultra only when needed — then heal failing tests.",
      subs: ["Lightning patching each step", "Escalating misses to Super / Ultra", "Verifying under v2 · heal ladder"] },
    { id: "report", name: "Prove & report", short: "Report", module: "audit/verifier · report", est: 12,
      desc: "Compare original and refactored code side by side — tests, outputs, schemas — re-check CVEs and compile the audit PDF.",
      subs: ["Parity: tests · outputs · schemas", "Re-checking CVEs on the new version", "Compiling the audit report"] },
  ];
  const STAGE_IDX = Object.fromEntries(STAGES.map((s, i) => [s.id, i]));

  /* ---------------------------------------------------------------- health / mode */
  async function detectMode() {
    if (params.has("demo") || CFG.DEMO === "on" || location.protocol === "file:") return (state.mode = "demo");
    try {
      const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 3000);
      const r = await fetch(API + "/api/health", { signal: ctl.signal }); clearTimeout(t);
      if (!r.ok) throw 0;
      state.mode = "live";
    } catch { state.mode = CFG.DEMO === "off" ? "live" : "demo"; }
  }

  /* ---------------------------------------------------------------- composer */
  const GH_RE = /^(?:https?:\/\/)?(?:www\.)?github\.com\/[\w.-]+\/[\w.-]+(?:\.git)?\/?$/i;

  function createComposer(mount, { compact = false, onSubmit } = {}) {
    const form = $("#tpl-composer").content.firstElementChild.cloneNode(true);
    if (compact) form.classList.add("compact");
    mount.appendChild(form);
    const ta = $(".composer-input", form), chips = $(".composer-chips", form), fileIn = $("input[type=file]", form);
    const sel = { file: null, repo: null };

    const grow = () => {
      ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
      ta.classList.toggle("is-code", /\n/.test(ta.value) || /^\s*(from|import|class|def|@)\b/m.test(ta.value));
    };
    const renderChips = () => {
      chips.innerHTML = "";
      if (sel.file) chips.appendChild(chip(`<small>file</small> <b>${esc(sel.file.name)}</b> <small>${(sel.file.size / 1024).toFixed(1)} KB</small>`, () => { sel.file = null; fileIn.value = ""; renderChips(); }));
      if (sel.repo) chips.appendChild(chip(`<small>repo</small> <b>${esc(sel.repo.replace(/^https?:\/\/(www\.)?github\.com\//, ""))}</b>`, () => { sel.repo = null; renderChips(); }));
    };
    function chip(html, onX) {
      const c = el("span", "in-chip", html);
      const x = el("button", "", "✕"); x.type = "button"; x.setAttribute("aria-label", "Remove"); x.onclick = onX;
      c.appendChild(x); return c;
    }
    const api = {
      form, ta,
      setFile(f) {
        if (!f) return;
        if (!/\.py$/i.test(f.name)) return toast("Only .py files are supported for now.", true);
        sel.file = f; sel.repo = null; renderChips(); ta.focus();
      },
      setRepo(url) { sel.repo = url; sel.file = null; renderChips(); ta.focus(); },
      setText(t) { ta.value = t; grow(); ta.focus(); },
      clear() { sel.file = sel.repo = null; ta.value = ""; fileIn.value = ""; renderChips(); grow(); },
      focus() { ta.focus(); },
      pickFile() { fileIn.click(); },
      async getInput() {
        const text = ta.value.trim();
        if (sel.file) return { kind: "file", file: sel.file, text: await sel.file.text(), label: sel.file.name };
        if (sel.repo) return { kind: "repo", url: sel.repo, text: sel.repo, label: repoName(sel.repo) };
        if (!text) return null;
        if (GH_RE.test(text.replace(/^["']|["']$/g, ""))) return { kind: "repo", url: text, text, label: repoName(text) };
        return { kind: "code", text, label: "your snippet" };
      },
    };
    ta.addEventListener("input", grow);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
    fileIn.addEventListener("change", () => api.setFile(fileIn.files[0]));
    $(".gh-btn", form).addEventListener("click", () => openGitHub(api));
    $(".judge-btn", form).addEventListener("click", openJudge);
    syncJudgeButtons();
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = await api.getInput();
      if (!input) { form.classList.remove("shake"); void form.offsetWidth; form.classList.add("shake"); ta.focus(); return; }
      onSubmit(input, api);
    });
    form.addEventListener("focusin", () => (state.activeComposer = api));
    return api;
  }
  const repoName = (u) => u.replace(/\.git$/, "").replace(/\/$/, "").split("/").pop();

  /* ---------------------------------------------------------------- overlays */
  function openOverlay(o) { o.classList.add("open"); o.setAttribute("aria-hidden", "false"); }
  function closeOverlay(o) { o.classList.remove("open"); o.setAttribute("aria-hidden", "true"); }
  document.addEventListener("click", (e) => { const c = e.target.closest("[data-close]"); if (c) closeOverlay(c.closest(".sheet, .drawer, .modal")); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $$(".sheet.open, .drawer.open, .modal.open").forEach(closeOverlay); });

  /* ---------------------------------------------------------------- FAQ */
  const FAQ = [
    ["What is Reforge?", "Reforge is an autonomous code-migration and vulnerability-remediation agent. Give it a Python repository or a snippet and it upgrades the code across a breaking dependency release, patches known CVEs, verifies every change in an isolated sandbox and hands back the refactored files with an audit report."],
    ["What can I give it?", "Three kinds of input: a public GitHub repository URL (or pick one with <em>Connect GitHub</em>), Pydantic v1 code pasted into the chat bar, or a <code>.py</code> file. Pasted snippets become their own test: Reforge records what the code prints under v1 and requires the migrated code to print exactly the same."],
    ["Which migrations are supported?", "The first target is <b>Pydantic v1 → v2</b> — validators, <code>Config</code> classes, <code>__root__</code> models, <code>parse_obj</code>/<code>.dict()</code>/<code>.json()</code>, field constraints and more. The pipeline itself (scan → break → ground → plan → patch → prove) is library-agnostic and built to take on further dependency upgrades."],
    ["How do I know the patch is correct?", "Nothing is trusted on faith. Reforge runs your tests under v1 for a baseline, breaks them under v2 in a sandbox, patches, and re-runs them under v2 with a self-healing loop. A final parity verifier compares the original and refactored trees side by side — test outcomes, return values, model schemas and entrypoint output — and flags any attempt to edit the tests themselves."],
    ["Which AI models does it use?", "NVIDIA Nemotron models on Nebius Token Factory, routed by difficulty: <b>Nemotron 3 Ultra</b> (550B MoE) writes the refactor plan once; <b>Nemotron 3.5 Lightning</b> (30B MoE) applies most patches; <b>Nemotron 3 Super</b> (120B MoE) and Ultra are only called when a cheaper tier misses or tests need healing."],
    ["What does a run cost?", "Cents. Every dispatch is logged to a cost ledger against an all-Ultra counterfactual, and Stage 4 is capped by a per-run budget (default $0.50). On the reference repository a full migration cost <b>$0.12 instead of $0.20</b> on Ultra alone; the patch stage on its own saved 88.5%."],
    ["Is my code kept anywhere?", "No. Workspaces, audit output and LLM reply caches are deleted when the server starts and shuts down. Tests run in throw-away virtual environments, and the telemetry file stores token counts only — never source code."],
    ["What do I get back?", "The refactored <code>.py</code> files (view them, diff them, download them), a git-apply-ready <code>reforge.patch</code>, and an audit PDF covering vulnerabilities resolved, the documentation each change was grounded in, a v1-vs-v2 performance matrix, the AI cost ledger and side-by-side diffs."],
    ["How long does it take?", "Usually around a minute for a small repository: the bulk is installing the two Pydantic versions and running the tests. Re-runs of the same repository are faster thanks to reusable sandboxes and a plan cache."],
    ["What can't Reforge do yet?", "Reforge is a focused first release, so a few things are out of scope today:<br>• <b>One migration:</b> only Pydantic v1 → v2. The pipeline is library-agnostic, but other upgrades (SQLAlchemy 2, NumPy 2 …) need their own detection rules and docs and are on the roadmap.<br>• <b>Python only</b>, and dependencies are read from <code>requirements.txt</code>; other package managers are not yet parsed.<br>• <b>Public GitHub repositories only</b> — no private repos, other hosts or local folders.<br>• <b>No pull requests:</b> it hands back the refactored files, a patch and a report, but doesn't push or open a PR.<br>• <b>Proof is only as strong as your tests:</b> code with few or no tests gets a weaker parity check (snippets are checked against their v1 output).<br>• <b>One run at a time</b> on a server, sized for small-to-medium repositories within a per-run model budget."],
  ];
  function fillFaq(list, firstOpen) {
    FAQ.forEach(([q, a], i) => {
      const it = el("div", "faq-item" + (firstOpen && i === 0 ? " open" : ""));
      const b = el("button", "faq-q", esc(q)); b.type = "button";
      b.onclick = () => it.classList.toggle("open");
      it.append(b, el("div", "faq-a", `<div><p>${a}</p></div>`));
      list.appendChild(it);
    });
  }
  function toggleFaqSection(force) {
    const sec = $("#faqSection"), btn = $("#faqToggle");
    const open = force ?? !sec.classList.contains("open");
    sec.classList.toggle("open", open); btn.setAttribute("aria-expanded", String(open));
    if (open) setTimeout(() => sec.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "start" }), 80);
  }
  function buildFaq() {
    fillFaq($("#faqList"), true);
    fillFaq($("#faqInline"), true);
    $("#faqToggle").onclick = () => toggleFaqSection();
  }

  /* ---------------------------------------------------------------- notice (nothing to migrate) */
  function showNotice(n) {
    $("#noticeTitle").textContent = n.title || "Nothing to migrate";
    $("#noticeMsg").textContent = n.message || "";
    $("#noticeIco").textContent = n.code === "up_to_date" ? "✓" : "!";
    $("#noticeIco").className = "notice-ico " + (n.code === "up_to_date" ? "ok" : "");
    $$(".sheet.open, .drawer.open").forEach(closeOverlay);
    showView("landing");
    setTimeout(() => openOverlay($("#notice")), currentView === "landing" ? 350 : 0);
  }
  // demo mode has no backend: a light client-side version of the same pre-flight check
  function demoPreflight(input) {
    if (input.kind === "repo") return null;
    const t = input.text, label = input.kind === "file" ? input.label : "This code";
    if (!/^\s*(from\s+pydantic(\.\w+)*\s+import|import\s+pydantic)\b/m.test(t))
      return { code: "no_pydantic", title: "No Pydantic code found", message: `${label} doesn't import Pydantic anywhere, so there's nothing to migrate. Reforge currently upgrades Pydantic v1 code to v2 — try a repository or snippet that uses Pydantic v1.` };
    const v1 = /@(root_)?validator\b|\bclass\s+Config\b|__root__|\.(parse_obj|parse_raw|from_orm|schema_json|__fields__)\b|\b(regex|min_items|max_items)\s*=|parse_obj_as|pydantic\.v1/.test(t);
    const v2 = /\b(field_validator|model_validator|ConfigDict|RootModel|TypeAdapter|model_dump|model_validate|model_config)\b/.test(t);
    if (v2 && !v1) return { code: "up_to_date", title: "Already on Pydantic v2", message: `${label} already uses Pydantic v2 APIs. No v1 APIs were found, so there's nothing to upgrade.` };
    return null;
  }

  /* ---------------------------------------------------------------- judge key + daily limits */
  const JUDGE_LS = "reforge.judgeKey";
  let judgeKey = "";
  try { judgeKey = localStorage.getItem(JUDGE_LS) || ""; } catch {}
  function setJudgeKey(k) {
    judgeKey = k || "";
    try { k ? localStorage.setItem(JUDGE_LS, k) : localStorage.removeItem(JUDGE_LS); } catch {}
    syncJudgeButtons();
  }
  function syncJudgeButtons() {
    $$(".judge-btn").forEach((b) => {
      b.classList.toggle("is-on", !!judgeKey);
      const sp = $("span", b); if (sp) sp.textContent = judgeKey ? "Judge mode" : "Judge key";
      b.title = judgeKey ? "Judge key active — click to change or remove" : "Enter a judge key";
    });
  }
  // The key field shows "*" per character (password inputs can only show dots); the real value lives in judgeRaw.
  let judgeRaw = "", judgeHidden = true;
  const jIn = $("#judgeInput");
  const paint = (caret) => { jIn.value = judgeHidden ? "*".repeat(judgeRaw.length) : judgeRaw; if (caret != null) jIn.setSelectionRange(caret, caret); };
  jIn.addEventListener("beforeinput", (e) => {
    if (!judgeHidden) return;                                   // revealed: normal editing, synced on "input"
    let s = jIn.selectionStart ?? judgeRaw.length, t = jIn.selectionEnd ?? s;
    const ins = e.inputType.startsWith("insert") ? (e.data ?? e.dataTransfer?.getData("text/plain") ?? "").replace(/\s+/g, "") : "";
    if (e.inputType === "deleteContentBackward" && s === t) s = Math.max(0, s - 1);
    else if (e.inputType === "deleteContentForward" && s === t) t = Math.min(judgeRaw.length, t + 1);
    else if (e.inputType.startsWith("delete") && e.inputType !== "deleteContentBackward" && e.inputType !== "deleteContentForward" && s === t) { s = 0; t = judgeRaw.length; }
    else if (!e.inputType.startsWith("insert") && !e.inputType.startsWith("delete")) return;
    e.preventDefault();
    judgeRaw = judgeRaw.slice(0, s) + ins + judgeRaw.slice(t);
    paint(s + ins.length);
  });
  jIn.addEventListener("input", () => { if (!judgeHidden) judgeRaw = jIn.value; else paint(); });
  jIn.addEventListener("copy", (e) => { if (judgeHidden) e.preventDefault(); });
  jIn.addEventListener("cut", (e) => { if (judgeHidden) e.preventDefault(); });
  function maskJudge(hidden) {
    judgeHidden = hidden; jIn.classList.toggle("masked", hidden); paint();
    const b = $("#judgeReveal");
    b.classList.toggle("on", !hidden); b.setAttribute("aria-pressed", String(!hidden));
    b.setAttribute("aria-label", hidden ? "Show key" : "Hide key"); b.title = hidden ? "Show key" : "Hide key";
  }
  $("#judgeReveal").addEventListener("click", () => { maskJudge(!judgeHidden); $("#judgeInput").focus(); });
  function openJudge() {
    judgeRaw = ""; maskJudge(true);
    $("#judgeClear").hidden = !judgeKey;
    $("#judgeCopy").textContent = judgeKey
      ? "Judge mode is active on this browser. Enter a different key, or remove it."
      : "Judges: enter the key from the Devpost testing instructions. It removes the public per-visitor run limit.";
    openOverlay($("#judgeModal"));
    setTimeout(() => $("#judgeInput").focus(), 200);
  }
  $("#judgeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const k = judgeRaw.trim();
    if (!k) return $("#judgeInput").focus();
    if (state.mode !== "live") return toast("Judge keys only work when the live backend is reachable.", true);
    const btn = $("#judgeForm button"); btn.disabled = true;
    try {
      const fd = new FormData(); fd.append("judge_key", k);
      const r = await fetch(API + "/api/judge/verify", { method: "POST", body: fd });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `Request failed (${r.status})`);
      setJudgeKey(k); closeOverlay($("#judgeModal")); toast("Judge mode on — public run limit removed.");
    } catch (err) {
      toast(err.message === "Failed to fetch" ? "Can't reach the Reforge backend." : err.message, true);
    } finally { btn.disabled = false; }
  });
  $("#judgeClear").addEventListener("click", () => { setJudgeKey(""); closeOverlay($("#judgeModal")); toast("Judge key removed."); });

  function showLimit(n) {
    $("#limitTitle").textContent = n.title || "Daily limit reached";
    $("#limitMsg").textContent = n.message || "Today's free live runs are used up. Watch the demo video to see a full run.";
    const url = n.video_url || CFG.DEMO_VIDEO_URL || "";
    const a = $("#limitVideo"); a.hidden = !url; if (url) a.href = url;
    $$(".sheet.open, .drawer.open, .modal.open").forEach(closeOverlay);
    showView("landing");
    setTimeout(() => openOverlay($("#limitModal")), currentView === "landing" ? 350 : 0);
  }

  /* ---------------------------------------------------------------- GitHub connect */
  let ghTarget = null;
  function openGitHub(composer) {
    ghTarget = composer; openOverlay($("#ghModal"));
    $("#ghResults").innerHTML = "";
    setTimeout(() => $("#ghInput").focus(), 200);
  }
  async function ghFetch(path) {
    const r = await fetch("https://api.github.com/" + path, { headers: { Accept: "application/vnd.github+json" } });
    if (r.status === 403) throw new Error("GitHub rate limit reached — paste the repository URL instead.");
    if (r.status === 404) throw new Error("Not found on GitHub (private repositories aren't supported yet).");
    if (!r.ok) throw new Error("GitHub request failed (" + r.status + ").");
    return r.json();
  }
  function pickRepo(fullName) {
    const url = "https://github.com/" + fullName;
    closeOverlay($("#ghModal"));
    (ghTarget || state.landing).setRepo(url);
    toast("Connected " + fullName);
  }
  function repoRow(r) {
    const b = el("button", "gh-repo");
    b.type = "button";
    b.innerHTML = `<div><b>${esc(r.full_name)}</b><small>${esc(r.description || "No description")}</small></div><span class="gh-lang ${r.language === "Python" ? "py" : ""}">${esc(r.language || "—")} · ★ ${r.stargazers_count ?? 0}</span>`;
    b.onclick = () => pickRepo(r.full_name);
    return b;
  }
  function initGitHub() {
    $("#ghForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const out = $("#ghResults"); const v = $("#ghInput").value.trim().replace(/\.git$/, "").replace(/\/$/, "");
      if (!v) return;
      const m = v.match(/(?:github\.com\/)?([\w.-]+)\/([\w.-]+)$/i);
      out.innerHTML = `<p class="gh-note">Looking up GitHub…</p>`;
      try {
        if (m && (v.includes("/"))) {
          const full = `${m[1]}/${m[2]}`;
          try { const r = await ghFetch("repos/" + full); out.innerHTML = ""; out.appendChild(repoRow(r)); }
          catch { pickRepo(full); }
          return;
        }
        const repos = await ghFetch(`users/${encodeURIComponent(v)}/repos?per_page=100&sort=updated`);
        repos.sort((a, b) => (b.language === "Python") - (a.language === "Python"));
        out.innerHTML = repos.length ? "" : `<p class="gh-note">No public repositories for ${esc(v)}.</p>`;
        repos.slice(0, 40).forEach((r, i) => { const row = repoRow(r); row.style.animationDelay = i * 25 + "ms"; out.appendChild(row); });
      } catch (err) { out.innerHTML = `<p class="gh-note">${esc(err.message)}</p>`; }
    });
  }

  /* ---------------------------------------------------------------- views */
  let currentView = "landing";
  function showView(name) {
    if (name === currentView) return;
    const swap = () => { $$(".view").forEach((v) => v.classList.toggle("is-active", v.id === "view-" + name)); currentView = name; };
    swap();
    if (name === "landing") startSparks();
  }

  /* ---------------------------------------------------------------- sparks */
  let sparksOn = false;
  function startSparks() {
    const cv = $("#sparks"); if (!cv || REDUCED || sparksOn) return;
    const ctx = cv.getContext("2d"); sparksOn = true;
    let w, h, dpr, P = [];
    const resize = () => { dpr = Math.min(devicePixelRatio || 1, 2); w = cv.clientWidth; h = cv.clientHeight; cv.width = w * dpr; cv.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); };
    resize(); addEventListener("resize", resize);
    const spawn = () => ({ x: Math.random() * w, y: h + 10, vx: (Math.random() - .5) * .25, vy: -(.25 + Math.random() * .7), r: .5 + Math.random() * 1.5, life: 0, max: 280 + Math.random() * 380, hue: 18 + Math.random() * 22 });
    const N = Math.round(Math.min(90, w / 16));
    for (let i = 0; i < N; i++) { const p = spawn(); p.y = Math.random() * h; p.life = Math.random() * p.max; P.push(p); }
    const tick = () => {
      if (currentView !== "landing") { sparksOn = false; return; }
      ctx.clearRect(0, 0, w, h); ctx.globalCompositeOperation = "lighter";
      for (const p of P) {
        p.life++; p.x += p.vx + Math.sin((p.life + p.hue) / 40) * .15; p.y += p.vy;
        const k = p.life / p.max, a = Math.max(0, (k < .1 ? k * 10 : 1 - k)) * (.35 + .65 * (p.y / h));
        ctx.beginPath(); ctx.fillStyle = `hsla(${p.hue}, 100%, ${55 + 20 * (1 - k)}%, ${a * .8})`;
        ctx.arc(p.x, p.y, p.r, 0, 6.283); ctx.fill();
        if (p.life >= p.max || p.y < -10) Object.assign(p, spawn());
      }
      requestAnimationFrame(tick);
    };
    tick();
  }

  /* ---------------------------------------------------------------- run */
  async function startRun(input, composer) {
    if (state.running) return toast("A run is already in progress.");
    state.input = input; state.result = null;
    let events;
    if (state.mode === "live") {
      const fd = new FormData();
      if (input.kind === "file") fd.append("code_file", input.file, input.file.name);
      else fd.append("content", input.kind === "repo" ? input.url : input.text);
      const btn = $(".send-btn", composer.form); btn.disabled = true;
      try {
        const res = await fetch(API + "/api/run-stream", { method: "POST", body: fd,
                                headers: judgeKey ? { "X-Reforge-Judge-Key": judgeKey } : {} });
        if (!res.ok) {
          const j = await res.json().catch(() => ({}));
          if (res.status === 429 && j.detail && typeof j.detail === "object") { btn.disabled = false; return showLimit(j.detail); }
          if (res.status === 403 && judgeKey) setJudgeKey("");
          if (j.detail && typeof j.detail === "object" && j.detail.code) { btn.disabled = false; composer.clear(); return showNotice(j.detail); }
          throw new Error(typeof j.detail === "string" ? j.detail : `Request failed (${res.status})`);
        }
        events = readNdjson(res);
      } catch (err) {
        btn.disabled = false;
        return toast(err.message === "Failed to fetch" ? "Can't reach the Reforge backend." : err.message, true);
      }
      btn.disabled = false;
    } else {
      const n = demoPreflight(input);
      if (n) { composer.clear(); return showNotice(n); }
      events = demoEvents(input);
    }
    composer.clear();
    runTrace(input, events);
  }

  async function* readNdjson(res) {
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (line) { try { yield JSON.parse(line); } catch { /* ignore partial */ } }
      }
    }
    if (buf.trim()) { try { yield JSON.parse(buf); } catch { } }
  }

  /* ---------------------------------------------------------------- trace */
  const T = { t0: 0, raf: 0, timers: [], cards: {}, lines: 0, stopped: false };

  function resetTrace(input) {
    T.timers.forEach(clearTimeout); T.timers = []; T.cards = {}; T.lines = 0; T.stopped = false;
    $("#traceTarget").textContent = input.label;
    $("#traceMode").textContent = state.mode === "demo" ? "demo" : "";
    $("#view-trace").scrollTop = 0;
    $("#stageStack").innerHTML = ""; $("#console").innerHTML = ""; $("#logCount").textContent = "0 lines";
    $("#railFill").style.width = "0"; $("#rail").classList.add("running");
    $("#finale").classList.remove("on"); $("#view-trace").classList.remove("finishing");
    $("#continueBtn").hidden = true; $("#continueBtn").classList.remove("on");
    ["costActual", "costUltra", "costSaved"].forEach((id) => ($("#" + id).textContent = "—"));
    $$(".tier").forEach((t) => { t.classList.remove("pulse"); $("i", t).style.width = "0"; $(".tier-count", t).textContent = "0"; });
    const nodes = $("#railNodes"); nodes.innerHTML = "";
    STAGES.forEach((s, i) => {
      const li = el("li", "rail-node", `<div class="rail-dot">${String(i).padStart(2, "0")}</div><div class="rail-label">${esc(s.short)}</div><div class="rail-time"></div>`);
      li.dataset.id = s.id; nodes.appendChild(li);
    });
    $("#clockSub").textContent = "elapsed";
  }

  function startClock() {
    T.t0 = performance.now(); cancelAnimationFrame(T.raf);
    const loop = () => { if (T.stopped) return; $("#clock").textContent = fmtClock((performance.now() - T.t0) / 1000); T.raf = requestAnimationFrame(loop); };
    loop();
  }
  function stopClock(label) { T.stopped = true; cancelAnimationFrame(T.raf); $("#clock").textContent = fmtClock((performance.now() - T.t0) / 1000); if (label) $("#clockSub").textContent = label; }

  function log(line, cls = "") {
    const c = $("#console"); const stick = c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
    const row = el("div", "log-line " + cls, `<time>${fmtClock((performance.now() - T.t0) / 1000)}</time><span>${esc(line)}</span>`);
    c.appendChild(row); T.lines++;
    if (c.children.length > 400) c.firstChild.remove();
    $("#logCount").textContent = `${T.lines} line${T.lines === 1 ? "" : "s"}`;
    if (stick) c.scrollTop = c.scrollHeight;
  }

  function railSet(id, status, elapsed) {
    const node = $(`.rail-node[data-id="${id}"]`); if (!node) return;
    node.classList.remove("running", "done", "failed"); node.classList.add(status);
    if (elapsed != null) $(".rail-time", node).textContent = elapsed.toFixed(1) + "s";
    const i = STAGE_IDX[id], n = STAGES.length - 1;
    const p = status === "done" ? Math.min(1, (i + 1) / n) : i / n;
    $("#railFill").style.width = Math.max(parseFloat($("#railFill").style.width) || 0, p * 100) + "%";
  }

  function stageCard(id) {
    const s = STAGES[STAGE_IDX[id]];
    const card = el("article", "stage-card running");
    card.innerHTML = `
      <div class="stage-num">${String(STAGE_IDX[id]).padStart(2, "0")}</div>
      <div class="stage-top"><h3 class="stage-name">${esc(s.name)}</h3><span class="stage-elapsed">running…</span></div>
      <div class="stage-module">${esc(s.module)}</div>
      <p class="stage-desc">${esc(s.desc)}</p>
      <ul class="subs">${s.subs.map((x) => `<li class="sub"><i></i>${esc(x)}</li>`).join("")}</ul>
      <div class="shimmer"></div>
      <div class="facts"></div>`;
    $("#stageStack").appendChild(card);
    requestAnimationFrame(() => card.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "nearest" }));
    // sub-steps advance on an estimate; the last one waits for the real "done" event
    const subs = $$(".sub", card), step = (s.est * 1000) / subs.length;
    subs[0].classList.add("active");
    for (let k = 1; k < subs.length; k++) {
      T.timers.push(setTimeout(() => { if (!card.classList.contains("running")) return; subs[k - 1].classList.replace("active", "done"); subs[k].classList.add("active"); }, step * k));
    }
    const started = performance.now();
    const tick = setInterval(() => { if (!card.classList.contains("running")) return clearInterval(tick); $(".stage-elapsed", card).textContent = ((performance.now() - started) / 1000).toFixed(1) + "s"; }, 100);
    T.cards[id] = card;
    return card;
  }

  function finishCard(id, ev) {
    const card = T.cards[id] || stageCard(id);
    card.classList.remove("running"); card.classList.add("done");
    $$(".sub", card).forEach((x) => { x.classList.remove("active"); x.classList.add("done"); });
    if (ev.elapsed != null) $(".stage-elapsed", card).textContent = ev.elapsed.toFixed(2) + "s";
    const facts = $(".facts", card);
    (ev.facts || []).forEach(([k, v], i) => { const f = el("div", "fact", `<small>${esc(k)}</small><b>${esc(v)}</b>`); f.style.animationDelay = i * 70 + "ms"; facts.appendChild(f); });
  }

  function setRouter(data) {
    const tiers = (data && data.tiers) || {}, cost = (data && data.cost) || {};
    const counts = { lightning: tiers.lightning || 0, super: tiers.super || 0, ultra: tiers.ultra || 0 };
    const max = Math.max(1, ...Object.values(counts));
    $$(".tier").forEach((t) => {
      t.classList.remove("pulse");
      const n = counts[t.dataset.tier]; $("i", t).style.width = (n / max) * 100 + "%";
      countUp($(".tier-count", t), n, 0);
    });
    $("#costActual").textContent = usd(cost.actual_usd);
    $("#costUltra").textContent = usd(cost.counterfactual_ultra_usd);
    $("#costSaved").textContent = cost.savings_pct != null ? cost.savings_pct + "%" : usd(cost.savings_usd);
  }
  function countUp(node, to, dec = 0) {
    const from = 0, t0 = performance.now(), d = 900;
    const f = (t) => { const k = Math.min(1, (t - t0) / d), e = 1 - Math.pow(1 - k, 3); node.textContent = (from + (to - from) * e).toFixed(dec); if (k < 1) requestAnimationFrame(f); };
    requestAnimationFrame(f);
  }

  async function runTrace(input, events) {
    state.running = true;
    resetTrace(input);
    showView("trace");
    await sleep(450);
    startClock();
    log(`reforge ▸ ${input.kind === "repo" ? "repository " + input.url : input.kind === "file" ? "file " + input.label : "pasted snippet (" + input.text.split("\n").length + " lines)"}`, "stage");
    let failed = false, halted = null;
    try {
      for await (const ev of events) {
        if (ev.type === "stage" && ev.status === "running") {
          railSet(ev.id, "running"); stageCard(ev.id);
          log(`▸ ${STAGES[STAGE_IDX[ev.id]].name}`, "stage");
          if (ev.id === "patch") $$(".tier").forEach((t) => t.dataset.tier === "lightning" && t.classList.add("pulse"));
          if (ev.id === "patch") T.timers.push(setTimeout(() => $(".tier[data-tier=super]")?.classList.add("pulse"), 5000));
        } else if (ev.type === "stage" && ev.status === "done") {
          railSet(ev.id, "done", ev.elapsed); finishCard(ev.id, ev);
          log(`✓ ${STAGES[STAGE_IDX[ev.id]].name} · ${(ev.elapsed ?? 0).toFixed(2)}s`, "ok");
          if (ev.id === "patch") setRouter(ev.data);
        } else if (ev.type === "log") {
          log(ev.line, /error|failed|traceback/i.test(ev.line) ? "err" : "");
        } else if (ev.type === "result") {
          state.result = ev.data;
        } else if (ev.type === "halt") {
          halted = ev; log("■ " + (ev.title || "Stopped") + " — nothing to migrate", "err");
        } else if (ev.type === "error") {
          failed = true; showError(ev.stage, ev.detail);
        }
      }
    } catch (err) {
      failed = true; showError(null, "Connection to the backend was lost: " + err.message);
    }
    state.running = false;
    $("#rail").classList.remove("running");
    if (halted) { stopClock("stopped"); await sleep(REDUCED ? 0 : 900); return showNotice(halted); }
    if (failed) return;
    if (!state.result) return showError(null, "The run ended without a result.");
    stopClock("total");
    log("✓ Run complete — review the trace, then press Continue", "stage");
    await waitContinue();
    await finale(state.result);
    renderResults(state.result, input);
    showView("results");
  }

  function showError(stageId, detail) {
    stopClock("stopped");
    $("#rail").classList.remove("running");
    log("✕ " + detail, "err");
    let card = stageId ? T.cards[stageId] : null;
    if (stageId) railSet(stageId, "failed");
    if (!card) { card = el("article", "stage-card"); card.innerHTML = `<div class="stage-num">!</div><div class="stage-top"><h3 class="stage-name">Run stopped</h3></div><div class="facts"></div>`; $("#stageStack").appendChild(card); }
    card.classList.remove("running", "done"); card.classList.add("failed");
    $$(".sub.active", card).forEach((s) => s.classList.remove("active"));
    const se = $(".stage-elapsed", card); if (se) se.textContent = "failed";
    const box = el("div", "err-box", esc(detail));
    const acts = el("div", "err-actions");
    const back = el("button", "ghost-btn", "← Back to start"); back.onclick = goHome;
    const retry = el("button", "solid-btn", "Retry"); retry.onclick = () => { if (state.input) runAgain(state.input); };
    acts.append(back, retry); card.append(box, acts);
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function runAgain(input) {
    if (state.mode === "live") {
      const fake = { form: document.createElement("form"), clear() {} };
      fake.form.appendChild(el("button", "send-btn"));
      return startRun(input, fake);
    }
    runTrace(input, demoEvents(input));
  }

  // Hold on the finished trace until the user presses Continue (bottom-right).
  function waitContinue() {
    const b = $("#continueBtn");
    b.hidden = false; requestAnimationFrame(() => b.classList.add("on"));
    return new Promise((resolve) => {
      b.onclick = () => { b.onclick = null; b.classList.remove("on"); b.hidden = true; resolve(); };
    });
  }

  async function finale(r) {
    const v = String(r.verdict || "").toUpperCase();
    $("#finaleEyebrow").textContent = v === "PASS" ? "Parity verified" : v === "WARN" ? "Parity · with warnings" : "Run complete";
    const sm = r.summary || {};
    $("#finaleSub").textContent = `${r.package || "pydantic"} ${r.from_version || ""} → ${r.to_version || ""} · ${sm.files_refactored ?? (r.files || []).length} files refactored · ${fmtClock(r.timings_s?.total ?? (performance.now() - T.t0) / 1000)}`;
    $("#finale").classList.add("on"); $("#view-trace").classList.add("finishing");
    await sleep(REDUCED ? 600 : 1900);
  }

  /* ---------------------------------------------------------------- results */
  const BOT_AV = `<div class="msg-av"><svg viewBox="0 0 32 32"><path d="M10 26V6h7.5a5.5 5.5 0 0 1 0 11H10" fill="none" stroke="currentColor" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/><path d="M16.5 17L23 26" fill="none" stroke="#ff6b2c" stroke-width="3" stroke-linecap="round"/></svg></div>`;

  function userMsg(input) {
    const m = el("div", "msg user");
    const inner = input.kind === "code" || input.kind === "file"
      ? `${input.kind === "file" ? `<div style="font-size:14px;color:var(--muted);margin-bottom:6px">📎 ${esc(input.label)}</div>` : ""}<pre>${esc(input.text.split("\n").slice(0, 10).join("\n"))}${input.text.split("\n").length > 10 ? "\n…" : ""}</pre>`
      : esc(input.text || input.url);
    m.innerHTML = `<div class="bubble">${inner}</div>`;
    $("#chatLog").appendChild(m); scrollChat();
  }
  async function botMsg(html, { delay = 650 } = {}) {
    const m = el("div", "msg bot", `${BOT_AV}<div class="body"><div class="typing"><i></i><i></i><i></i></div></div>`);
    $("#chatLog").appendChild(m); scrollChat();
    await sleep(REDUCED ? 0 : delay);
    const body = $(".body", m); body.innerHTML = html; body.style.animation = "rise .5s var(--ease) both";
    scrollChat();
    return body;
  }
  const scrollChat = () => { const s = $("#chatScroll"); requestAnimationFrame(() => (s.scrollTop = s.scrollHeight)); };

  function verdictClass(v) { v = String(v || "").toUpperCase(); return v === "PASS" ? "pass" : v === "WARN" ? "warn" : v ? "fail" : ""; }

  function summaryHtml(r) {
    const sm = r.summary || {}, v = String(r.verdict || "").toUpperCase();
    const head = v === "PASS" ? "Migration verified." : v === "WARN" ? "Migrated — with notes." : v === "N/A" ? "Patched." : "Needs a closer look.";
    const files = r.files || [];
    const add = files.reduce((a, f) => a + (f.added || 0), 0), rem = files.reduce((a, f) => a + (f.removed || 0), 0);
    const tb = r.tests?.before || {}, ta = r.tests?.after || {};
    const cost = r.patch?.cost || {};
    const spend = sm.ai_spend_usd ?? cost.actual_usd, saved = sm.saved_usd ?? cost.savings_usd;
    const ultra = spend != null && saved != null ? +spend + +saved : cost.counterfactual_ultra_usd;
    const savedPct = ultra ? Math.round((saved / ultra) * 100) : cost.savings_pct;
    const kpis = [
      ["Test pass rate", sm.pass_rate_after != null ? pct(sm.pass_rate_after) : ta.ok ? "100%" : "—", `v1 baseline ${tb.passed ?? "?"}/${tb.collected ?? "?"} · was ${pct(sm.pass_rate_before)}`],
      ["Vulnerabilities", `${sm.vulnerabilities_resolved ?? 0}/${sm.vulnerabilities_total ?? r.cve_count ?? 0}`, "resolved, re-checked on OSV"],
      ["Output parity", sm.output_parity_pct != null ? pct(sm.output_parity_pct) : "—", "original vs refactored"],
      ["AI spend", usd(spend), savedPct != null ? `${savedPct}% below Ultra-only` : "Nebius Token Factory"],
    ];
    if (sm.test_time_speedup_x) kpis.push(["Hot-path speedup", (+sm.test_time_speedup_x).toFixed(2) + "×", "v2 vs v1 test time"]);
    const timings = Object.entries(r.timings_s || {}).filter(([k]) => k !== "total");
    const total = r.timings_s?.total || timings.reduce((a, [, v]) => a + v, 0) || 1;
    const nice = (k) => k.replace(/^stage\d_?/, "").replace(/_/g, " ");
    return `
      <h4>${head}</h4>
      <p>I upgraded <b>${r.input_type === "text" ? "your snippet" : esc(r.ws)}</b> from <code>${esc(r.package || "pydantic")} ${esc(r.from_version || "1.x")}</code> to <code>${esc(r.to_version || "2.x")}</code>.
      ${r.plan?.steps?.length ? `Nemotron 3 Ultra wrote a ${r.plan.steps.length}-step plan; ` : ""}${files.length} file${files.length === 1 ? "" : "s"} refactored (<span style="color:var(--ok)">+${add}</span> / <span style="color:var(--bad)">−${rem}</span>)${r.patch?.status ? `, patch status <code>${esc(r.patch.status)}</code>` : ""}.</p>
      <div class="kpis">${kpis.map(([k, v, s]) => `<div class="kpi"><small>${esc(k)}</small><b>${esc(v)}</b><span>${esc(s)}</span></div>`).join("")}</div>
      ${timings.length ? `<div class="timeline"><div class="tl-bar">${timings.map(([k, v]) => `<i title="${esc(nice(k))}: ${v}s" style="flex:${Math.max(v, total * 0.01)}"></i>`).join("")}</div>
        <div class="tl-legend">${timings.map(([k, v]) => `<span>${esc(nice(k))} <b>${(+v).toFixed(1)}s</b></span>`).join("")}<span>total <b>${(+total).toFixed(1)}s</b></span></div></div>` : ""}
      ${(r.reasons || []).length ? `<ul>${r.reasons.slice(0, 3).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      <p style="color:var(--ink-2)">The refactored files and the audit report are on the right. Ask me about the changes, the cost or the tests — or drop in another repository.</p>
      <div class="suggests">
        <button class="chip" data-ask="What changed?">What changed?</button>
        <button class="chip" data-ask="Cost breakdown">Cost breakdown</button>
        <button class="chip" data-ask="How were the tests?">Tests</button>
        <button class="chip" data-ask="Which vulnerabilities were fixed?">Vulnerabilities</button>
        <button class="chip" data-ask="Show the plan">The plan</button>
        <button class="chip" data-ask="Open the report">Open report</button>
      </div>`;
  }

  function renderResults(r, input) {
    $("#chatLog").innerHTML = "";
    userMsg(input);
    botMsg(summaryHtml(r), { delay: 900 });
    // outputs
    $("#outTitle").textContent = r.input_type === "text" ? "Your snippet" : r.ws || "Run artifacts";
    const vb = $("#outVerdict"); vb.textContent = r.verdict ? "Parity " + r.verdict : "—"; vb.className = "verdict " + verdictClass(r.verdict);
    const meta = [`${r.package || "pydantic"} ${r.from_version || ""} → ${r.to_version || ""}`, r.input_type === "github" ? "GitHub repo" : "Snippet", state.mode === "demo" ? "Demo data" : null, r.timings_s?.total ? `${(+r.timings_s.total).toFixed(1)}s` : null].filter(Boolean);
    $("#outMeta").innerHTML = meta.map((m) => `<span>${esc(m)}</span>`).join("");
    const fl = $("#fileList"); fl.innerHTML = "";
    if (!(r.files || []).length) fl.innerHTML = `<li class="empty-note">No files needed changes.</li>`;
    (r.files || []).forEach((f, i) => fl.appendChild(fileRow({
      ico: ext(f.file) || "txt", title: base(f.file), sub: f.file, delay: 300 + i * 80,
      side: `<span class="delta"><span class="a">+${f.added || 0}</span><span class="r">−${f.removed || 0}</span></span>`,
      open: () => openCode(f), download: () => downloadCode(f),
    })));
    const rl = $("#reportList"); rl.innerHTML = "";
    rl.appendChild(fileRow({ ico: "pdf", cls: "pdf", title: "Audit report", sub: "report.pdf · CVEs, docs, performance, cost, diffs", delay: 500,
      open: () => openPdf(r), download: () => downloadReport(r) }));
    if ((r.files || []).length) rl.appendChild(fileRow({ ico: "diff", cls: "patch", title: "reforge.patch", sub: "git apply-ready unified diff", delay: 580,
      open: () => openPatch(r), download: () => downloadPatch(r) }));
    $("#dlAllBtn").onclick = () => downloadAll(r);
  }

  function fileRow({ ico, cls = "", title, sub, side = "", delay = 0, open, download }) {
    const li = el("li", "file-row"); li.style.animationDelay = delay + "ms";
    const b = el("button", "file-item"); b.type = "button";
    b.innerHTML = `<span class="file-ico ${cls}">${esc(ico)}</span><span class="file-main"><b>${esc(title)}</b><small>${esc(sub)}</small></span>${side}`;
    b.onclick = open;
    const d = el("button", "dl-btn", `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M5 19.5h14" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>`);
    d.type = "button"; d.title = "Download " + title; d.setAttribute("aria-label", "Download " + title);
    d.onclick = download;
    li.append(b, d); return li;
  }

  /* ---------------------------------------------------------------- downloads */
  const patchText = (r) => (r.files || []).map((f) => f.unified || unifiedDiff(f.before || "", f.after || "", f.file)).join("");
  const reportSrc = (r) => (state.mode === "demo" ? "assets/demo-report.pdf" : abs(r.report_url));
  function downloadCode(f) { saveBlob(f.after || "", base(f.file), ext(f.file) === "py" ? "text/x-python" : "text/plain"); toast("Downloaded " + base(f.file)); }
  function downloadPatch(r) { saveBlob(patchText(r), `reforge_${r.ws || "run"}.patch`, "text/x-diff"); toast("Downloaded reforge.patch"); }
  async function fetchBlob(url) { const res = await fetch(url); if (!res.ok) throw new Error(`HTTP ${res.status}`); return res.blob(); }
  async function downloadReport(r) {
    try { saveBlobRaw(await fetchBlob(reportSrc(r)), `reforge_${r.ws || "report"}.pdf`); toast("Downloaded audit report"); }
    catch (e) { toast("Couldn't download the report: " + e.message, true); }
  }
  async function downloadAll(r) {
    if (!window.JSZip) return toast("Zip library didn't load — download the files one by one.", true);
    const btn = $("#dlAllBtn"); btn.disabled = true; const label = btn.textContent; btn.textContent = "Packing…";
    try {
      const zip = new JSZip(), root = zip.folder(`reforge_${r.ws || "run"}`);
      (r.files || []).forEach((f) => root.file("refactored/" + f.file, f.after || ""));
      if ((r.files || []).length) root.file("reforge.patch", patchText(r));
      try { root.file("report.pdf", await fetchBlob(reportSrc(r))); } catch { toast("Report couldn't be added to the zip.", true); }
      saveBlobRaw(await zip.generateAsync({ type: "blob" }), `reforge_${r.ws || "run"}.zip`);
    } finally { btn.disabled = false; btn.textContent = label; }
  }

  // chat answers come from the run's own data (no extra model calls, no extra credits)
  function answer(q) {
    const r = state.result || {}; q = q.toLowerCase();
    const files = r.files || [];
    if (/report|pdf|audit/.test(q)) { openPdf(r); return `<p>Opening the audit report. It covers the vulnerabilities resolved, the documentation each change was grounded in, a v1-vs-v2 performance matrix, the cost ledger and side-by-side diffs.</p>`; }
    if (/cost|spend|price|cheap|saving|\$|token|model|tier|router/.test(q)) {
      const c = r.patch?.cost || {}, t = r.patch?.tiers || {}, sm = r.summary || {};
      return `<p>Total AI spend for this run was <b>${usd(sm.ai_spend_usd ?? c.actual_usd)}</b>, saving <b>${usd(sm.saved_usd ?? c.savings_usd)}</b> against sending every call to Nemotron 3 Ultra.</p>
        <ul><li>Patch stage: ${usd(c.actual_usd)} vs ${usd(c.counterfactual_ultra_usd)} Ultra-only (${c.savings_pct ?? "—"}% saved, ${c.dispatches ?? "?"} dispatches)</li>
        <li>Final tier per step: ${Object.entries(t).map(([k, v]) => `${esc(k)} ×${v}`).join(", ") || "—"}</li>
        <li>The plan itself is one Ultra call${r.plan?.mode ? ` (mode <code>${esc(r.plan.mode)}</code>)` : ""}; everything else starts on Lightning and escalates only on a miss.</li></ul>`;
    }
    if (/test|pass|fail|break|broke/.test(q)) {
      const tb = r.tests?.before || {}, ta = r.tests?.after || {}, sm = r.summary || {};
      return `<p>Under v1 the baseline was <b>${tb.passed ?? "?"}/${tb.collected ?? "?"}</b> passing. Installing v2 broke <b>${r.tests?.newly_broken ?? "?"}</b> test${r.tests?.newly_broken === 1 ? "" : "s"}. After patching, the pass rate is <b>${pct(sm.pass_rate_after)}</b>${ta.ok != null ? ` and the v2 verification ${ta.ok ? "passed" : "did not pass"}` : ""}.</p>
        ${(r.breakage_symbols || []).length ? `<p>Symbols that broke: ${r.breakage_symbols.slice(0, 12).map((s) => `<code>${esc(s)}</code>`).join(" ")}</p>` : ""}`;
    }
    if (/cve|vuln|secur|advis|osv/.test(q)) {
      const v = r.vulnerabilities || [];
      if (!v.length) return `<p>No known advisories were attached to the original version${r.cve_count ? ` (scanner counted ${r.cve_count})` : ""}. The report re-checks the new version on OSV.dev.</p>`;
      return `<p>${v.length} advisor${v.length === 1 ? "y" : "ies"} affected <code>${esc(r.package)} ${esc(r.from_version)}</code>; moving to <code>${esc(r.to_version)}</code> resolves ${r.summary?.vulnerabilities_resolved ?? "them"}:</p><ul>${v.map((x) => typeof x === "string" ? `<li><code>${esc(x)}</code></li>` : `<li><code>${esc(x.id || x.cve || x.alias || "advisory")}</code> ${esc(x.summary || x.details || "")}</li>`).join("")}</ul>`;
    }
    if (/plan|step/.test(q)) {
      const st = r.plan?.steps || [];
      if (!st.length) return `<p>No plan steps were recorded for this run.</p>`;
      return `<p>The refactor plan had <b>${st.length}</b> steps:</p><ul>${st.slice(0, 14).map((s) => `<li><code>${esc(s.file)}</code> — ${esc(s.title || s.action || "edit")}</li>`).join("")}</ul>${st.length > 14 ? `<p>…and ${st.length - 14} more.</p>` : ""}`;
    }
    if (/time|long|fast|slow|speed|duration/.test(q)) {
      const t = Object.entries(r.timings_s || {});
      return `<p>Stage timings:</p><ul>${t.map(([k, v]) => `<li>${esc(k.replace(/_/g, " "))}: <b>${(+v).toFixed(2)}s</b></li>`).join("")}</ul>`;
    }
    if (/chang|diff|file|what|modif|refactor/.test(q)) {
      if (!files.length) return `<p>No files needed changes.</p>`;
      setTimeout(() => $$("#chatLog .ask-file").forEach((b) => (b.onclick = () => openCode(files[+b.dataset.i]))), 50);
      return `<p>${files.length} file${files.length === 1 ? " was" : "s were"} refactored:</p><ul>${files.map((f, i) => `<li><code>${esc(f.file)}</code> <span style="color:var(--ok)">+${f.added}</span> <span style="color:var(--bad)">−${f.removed}</span> · <a href="#" class="ask-file" data-i="${i}" onclick="return false">view diff</a></li>`).join("")}</ul>`;
    }
    return `<p>I can walk you through <b>what changed</b>, the <b>cost breakdown</b>, the <b>tests</b>, the <b>vulnerabilities</b>, the <b>plan</b> or the <b>timings</b> — or paste another GitHub URL or Pydantic v1 snippet and I'll start a new run.</p>`;
  }

  const looksLikeQuestion = (input) => input.kind === "code" && !/\n/.test(input.text) && !/^(from|import|class|def|@)\b/.test(input.text) && !/[=:(]\s*\w/.test(input.text.replace(/[?!.,]$/, "")) && input.text.length < 220;

  function onChatSubmit(input, composer) {
    if (looksLikeQuestion(input)) {
      composer.clear(); userMsg(input); botMsg(answer(input.text), { delay: 550 });
      return;
    }
    startRun(input, composer);
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("[data-ask]"); if (!a) return;
    const q = a.dataset.ask; userMsg({ kind: "code", text: q }); botMsg(answer(q), { delay: 500 });
  });

  /* ---------------------------------------------------------------- viewer */
  function highlightLine(line, lang) {
    if (lang === "python" && window.hljs) { try { return hljs.highlight(line, { language: "python", ignoreIllegals: true }).value; } catch { } }
    return esc(line);
  }
  function codeHtml(text, lang) {
    return `<pre class="code">${String(text).replace(/\n$/, "").split("\n").map((l) => `<div class="ln"><span>${highlightLine(l, lang) || " "}</span></div>`).join("")}</pre>`;
  }
  function diffHtml(unified, lang) {
    let a = 0, b = 0; const rows = [];
    for (const l of String(unified).split("\n")) {
      if (/^(diff --git|index |--- |\+\+\+ )/.test(l)) { rows.push(`<div class="ln meta">${esc(l)}</div>`); continue; }
      const h = l.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)/);
      if (h) { a = +h[1]; b = +h[2]; rows.push(`<div class="ln hunk">${esc(l)}</div>`); continue; }
      if (l.startsWith("\\")) { rows.push(`<div class="ln meta">${esc(l)}</div>`); continue; }
      const k = l[0], body = highlightLine(l.slice(1), lang) || " ";
      if (k === "+") rows.push(`<div class="ln add"><span class="no"></span><span class="no">${b++}</span><span class="mark">+</span><span>${body}</span></div>`);
      else if (k === "-") rows.push(`<div class="ln del"><span class="no">${a++}</span><span class="no"></span><span class="mark">−</span><span>${body}</span></div>`);
      else if (k === " ") rows.push(`<div class="ln"><span class="no">${a++}</span><span class="no">${b++}</span><span class="mark"></span><span>${body}</span></div>`);
    }
    return `<pre class="code diff">${rows.join("") || `<div class="ln meta">No differences.</div>`}</pre>`;
  }

  function openSheet({ title, sub, ico, icoCls = "", tabs, download, openUrl }) {
    $("#sheetTitle").textContent = title; $("#sheetSub").textContent = sub || "";
    const i = $("#sheetIco"); i.textContent = ico; i.className = "file-ico " + icoCls;
    const tabBox = $("#sheetTabs"), body = $("#sheetBody"); tabBox.innerHTML = "";
    const show = (k) => { $$("button", tabBox).forEach((b) => b.classList.toggle("on", b.dataset.k === k)); body.innerHTML = tabs.find((t) => t.k === k).html(); body.scrollTop = 0; };
    if (tabs.length > 1) tabs.forEach((t) => { const b = el("button", "", esc(t.label)); b.type = "button"; b.dataset.k = t.k; b.onclick = () => show(t.k); tabBox.appendChild(b); });
    show(tabs[0].k);
    $("#sheetDownload").onclick = download;
    const o = $("#sheetOpen"); o.hidden = !openUrl; if (openUrl) o.href = openUrl;
    openOverlay($("#sheet"));
  }
  function saveBlob(text, name, type = "text/plain") { saveBlobRaw(new Blob([text], { type }), name); }
  function saveBlobRaw(blob, name) {
    const a = el("a"); a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }
  function openCode(f) {
    const lang = ext(f.file) === "py" ? "python" : "plain";
    const unified = f.unified || unifiedDiff(f.before || "", f.after || "", f.file);
    openSheet({
      title: base(f.file), sub: `${f.file} · +${f.added || 0} −${f.removed || 0}`, ico: ext(f.file) || "txt",
      tabs: [
        { k: "diff", label: "Changes", html: () => diffHtml(unified, lang) },
        { k: "after", label: "Refactored", html: () => codeHtml(f.after || "", lang) },
        { k: "before", label: "Original", html: () => codeHtml(f.before || "", lang) },
      ],
      download: () => downloadCode(f),
    });
  }
  function openPdf(r) {
    const demo = state.mode === "demo";
    const view = demo ? "assets/demo-report.pdf" : abs(r.report_view_url || r.report_url);
    openSheet({
      title: "Audit report", sub: `report.pdf · ${r.ws || ""}`, ico: "pdf", icoCls: "pdf",
      tabs: [{ k: "pdf", label: "PDF", html: () => `<iframe src="${esc(view)}#view=FitH" title="Audit report"></iframe>` }],
      download: () => downloadReport(r),
      openUrl: view,
    });
  }
  function openPatch(r) {
    const text = (r.files || []).map((f) => f.unified || unifiedDiff(f.before || "", f.after || "", f.file)).join("");
    openSheet({
      title: "reforge.patch", sub: `${(r.files || []).length} files · git apply reforge.patch`, ico: "diff", icoCls: "patch",
      tabs: [{ k: "p", label: "Patch", html: () => diffHtml(text, "python") }],
      download: () => downloadPatch(r),
    });
  }

  /* ---------------------------------------------------------------- unified diff (client-side, for demo / fallback) */
  function unifiedDiff(before, after, file, ctx = 3) {
    const a = before.replace(/\r\n/g, "\n").split("\n"), b = after.replace(/\r\n/g, "\n").split("\n");
    if (a[a.length - 1] === "") a.pop(); if (b[b.length - 1] === "") b.pop();
    const n = a.length, m = b.length;
    if (n * m > 4e6) return `diff --git a/${file} b/${file}\n--- a/${file}\n+++ b/${file}\n@@ -1,${n} +1,${m} @@\n` + a.map((l) => "-" + l).join("\n") + "\n" + b.map((l) => "+" + l).join("\n") + "\n";
    const L = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    const ops = []; let i = 0, j = 0;
    while (i < n && j < m) { if (a[i] === b[j]) { ops.push([" ", a[i], i, j]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) { ops.push(["-", a[i], i, j]); i++; } else { ops.push(["+", b[j], i, j]); j++; } }
    while (i < n) { ops.push(["-", a[i], i, j]); i++; } while (j < m) { ops.push(["+", b[j], i, j]); j++; }
    const changed = ops.map((o, k) => (o[0] !== " " ? k : -1)).filter((k) => k >= 0);
    if (!changed.length) return "";
    let out = `diff --git a/${file} b/${file}\n--- a/${file}\n+++ b/${file}\n`, k = 0;
    while (k < changed.length) {
      let s = Math.max(0, changed[k] - ctx), e = changed[k];
      while (k < changed.length && changed[k] <= e + 2 * ctx) { e = changed[k]; k++; }
      e = Math.min(ops.length - 1, e + ctx);
      const seg = ops.slice(s, e + 1);
      const aStart = seg[0][2] + 1, bStart = seg[0][3] + 1;
      const aLen = seg.filter((o) => o[0] !== "+").length, bLen = seg.filter((o) => o[0] !== "-").length;
      out += `@@ -${aStart},${aLen} +${bStart},${bLen} @@\n` + seg.map((o) => o[0] + o[1]).join("\n") + "\n";
    }
    return out;
  }

  /* ---------------------------------------------------------------- demo data */
  const V1_SAMPLE = `from typing import List, Optional
from pydantic import BaseModel, Field, validator, root_validator, parse_obj_as


class Address(BaseModel):
    city: str
    zip_code: str = Field(..., regex=r"^\\d{6}$")


class User(BaseModel):
    id: int
    name: str = Field(..., alias="userName")
    tags: List[str] = Field(default_factory=list, max_items=5)
    address: Optional[Address] = None

    class Config:
        orm_mode = True
        allow_population_by_field_name = True
        anystr_lower = True

    @validator("name")
    def name_not_blank(cls, v):
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.title()

    @root_validator(pre=True)
    def default_tags(cls, values):
        values.setdefault("tags", ["new"])
        return values


class UserList(BaseModel):
    __root__: List[User]


users = parse_obj_as(List[User], [{"id": 1, "userName": "asha", "address": {"city": "Pune", "zip_code": "411001"}}])
team = UserList.parse_obj([u.dict() for u in users])
print(users[0].json(exclude_none=True))
print(list(User.__fields__), team.__root__[0].copy(update={"id": 2}).dict(by_alias=True))
`;
  const V2_SAMPLE = `from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter, field_validator, model_validator


class Address(BaseModel):
    city: str
    zip_code: str = Field(..., pattern=r"^\\d{6}$")


class User(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True, str_to_lower=True)

    id: int
    name: str = Field(..., alias="userName")
    tags: List[str] = Field(default_factory=list, max_length=5)
    address: Optional[Address] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v):
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.title()

    @model_validator(mode="before")
    @classmethod
    def default_tags(cls, values):
        values.setdefault("tags", ["new"])
        return values


class UserList(RootModel[List[User]]):
    pass


users = TypeAdapter(List[User]).validate_python([{"id": 1, "userName": "asha", "address": {"city": "Pune", "zip_code": "411001"}}])
team = UserList.model_validate([u.model_dump() for u in users])
print(users[0].model_dump_json(exclude_none=True))
print(list(User.model_fields), team.root[0].model_copy(update={"id": 2}).model_dump(by_alias=True))
`;
  const MODELS_V1 = V1_SAMPLE.split("\n\n\nusers =")[0] + "\n";
  const MODELS_V2 = V2_SAMPLE.split("\n\n\nusers =")[0] + "\n";
  const API_V1 = `from typing import List

from app.models import User, UserList


def load_users(rows: List[dict]) -> UserList:
    return UserList.parse_obj(rows)


def serialize(user: User) -> str:
    return user.json(by_alias=True, exclude_none=True)


def schema() -> dict:
    return User.schema()


def rename(user: User, name: str) -> dict:
    return user.copy(update={"name": name}).dict()


def first(team: UserList) -> User:
    return team.__root__[0]
`;
  const API_V2 = API_V1.replace("UserList.parse_obj(rows)", "UserList.model_validate(rows)")
    .replace("user.json(by_alias=True, exclude_none=True)", "user.model_dump_json(by_alias=True, exclude_none=True)")
    .replace("User.schema()", "User.model_json_schema()")
    .replace('user.copy(update={"name": name}).dict()', 'user.model_copy(update={"name": name}).model_dump()')
    .replace("team.__root__[0]", "team.root[0]");

  function demoFiles(input) {
    const mk = (file, before, after) => {
      const unified = unifiedDiff(before, after, file);
      const lines = unified.split("\n");
      return { file, before, after, unified, added: lines.filter((l) => l.startsWith("+") && !l.startsWith("+++")).length, removed: lines.filter((l) => l.startsWith("-") && !l.startsWith("---")).length };
    };
    if (input.kind !== "repo") return [mk("snippet.py", V1_SAMPLE, V2_SAMPLE)];
    return [mk("app/models.py", MODELS_V1, MODELS_V2), mk("app/api.py", API_V1, API_V2), mk("requirements.txt", "pydantic==1.10.12\npytest>=7\n", "pydantic==2.14.1\npytest>=7\n")];
  }

  async function* demoEvents(input) {
    const repo = input.kind === "repo";
    const ws = repo ? repoName(input.url) : "text";
    const files = demoFiles(input);
    const tests = repo ? [6, 6] : [1, 1];
    const plan = repo
      ? [["requirements.txt", "Pin pydantic==2.14.1"], ["app/models.py", "Field(regex=) → Field(pattern=)"], ["app/models.py", "class Config → model_config = ConfigDict(...)"], ["app/models.py", "max_items → max_length"], ["app/models.py", "@validator → @field_validator + @classmethod"], ["app/models.py", "@root_validator(pre=True) → @model_validator(mode='before')"], ["app/models.py", "__root__ model → RootModel[List[User]]"], ["app/api.py", "parse_obj → model_validate"], ["app/api.py", ".json() → .model_dump_json()"], ["app/api.py", ".schema() → .model_json_schema()"], ["app/api.py", ".copy().dict() → .model_copy().model_dump()"], ["app/api.py", "__root__ → .root"]]
      : [["snippet.py", "Field(regex=) → Field(pattern=)"], ["snippet.py", "Config → ConfigDict"], ["snippet.py", "max_items → max_length"], ["snippet.py", "@validator → @field_validator"], ["snippet.py", "@root_validator → @model_validator"], ["snippet.py", "__root__ → RootModel"], ["snippet.py", "parse_obj_as → TypeAdapter"], ["snippet.py", "dict/json/copy → model_* API"], ["snippet.py", "__fields__ → model_fields"]];
    const tiers = repo ? { lightning: 9, super: 2, ultra: 1 } : { lightning: 8, super: 1 };
    const cost = repo ? { actual_usd: 0.0812, counterfactual_ultra_usd: 0.2828, savings_usd: 0.2016, savings_pct: 71.3, dispatches: 26 } : { actual_usd: 0.0081, counterfactual_ultra_usd: 0.0708, savings_usd: 0.0627, savings_pct: 88.5, dispatches: 9 };
    const T = { ingest: 1.4, scan: 2.6, break: 3.0, plan: 3.4, patch: 4.2, report: 2.8 };
    const L = {
      ingest: repo ? [`[DEBUG] Normalized URL result: ${input.url}`, `[DEBUG] Workspace ${ws}: cloned`] : ["[DEBUG] Normalized URL result: None", "[DEBUG] Workspace text: snippet", "harness: generated test_snippet.py (golden stdout check)"],
      scan: ["scanner: pydantic 1.10.12 detected", "osv: 1 advisory affects pydantic 1.10.12", `harness: ${tests[0]} tests collected · ${tests[1]} passed under v1`],
      break: ["sandbox: installing pydantic==2.14.1 into .sandbox_venv", `sandbox: v2 break run → ${tests[0]} newly broken`, `slicer: ${files.filter((f) => f.file.endsWith(".py")).length} affected file(s) · ${plan.length + 2} findings · import-graph ordered`],
      plan: ["grounder: 9 official docs pages matched (docs.pydantic.dev/latest/migration)", `planner: Nemotron 3 Ultra → ${plan.length}-step refactor_plan`, "planner: guardrails ok · 0 dropped steps"],
      patch: ["switchyard: Lightning batch → " + files[0].file, "patcher: SEARCH/REPLACE exact-once match · ast gate ok", "switchyard: 1 miss → escalate Super", "verify: running tests under pydantic 2.14.1", `verify: ${tests[0]}/${tests[0]} passed · no regressions`, `ledger: $${cost.actual_usd} vs $${cost.counterfactual_ultra_usd} all-Ultra`],
      report: ["verifier: before/after trees prepared", "verifier: parity PASS · outputs identical · schemas match", "osv: 0 open advisories for pydantic 2.14.1", `report: data/audit/${ws}/report.pdf`],
    };
    const F = {
      ingest: [["Input", repo ? "GitHub repository" : "Code snippet"], ["Workspace", `${ws} (${repo ? "cloned" : "snippet"})`]],
      scan: [["Package", "pydantic 1.10.12 → 2.14.1"], ["Known CVEs", "1"], ["Baseline tests (v1)", `${tests[1]}/${tests[0]} passing`]],
      break: [["Break run (v2)", "tests_failing"], ["Newly broken tests", String(tests[0])], ["Affected files", String(files.length)], ["Breaking symbols", "validator, root_validator, Config, __root__, parse_obj …"]],
      plan: [["Refactor steps", String(plan.length)], ["Planner", "Nemotron 3 Ultra · llm"], ["Grounding", "docs: 9, symbols: 11"]],
      patch: [["Steps applied", `${plan.length}/${plan.length}`], ["Status", "verified · escalated"], ["Tier mix", Object.entries(tiers).map(([k, v]) => `${k} ×${v}`).join(", ")], ["Spend vs Ultra-only", `${usd(cost.actual_usd)} vs ${usd(cost.counterfactual_ultra_usd)}`]],
      report: [["Parity verdict", "PASS"], ["Files refactored", String(files.length)], ["Saved vs Ultra-only", usd(repo ? 0.2016 : 0.0627)]],
    };
    yield { type: "hello" };
    const timings = {};
    const keys = { ingest: "input + clone", scan: "stage1_scan_baseline", break: "stage2_break_run", plan: "stage3_ground_plan", patch: "stage4_patch_verify", report: "stage5_parity_report" };
    for (const s of STAGES) {
      yield { type: "stage", id: s.id, status: "running" };
      const lines = L[s.id], dur = T[s.id] * 1000;
      for (const ln of lines) { await sleep(dur / (lines.length + 1)); yield { type: "log", line: ln }; }
      await sleep(dur / (lines.length + 1));
      const el2 = +(T[s.id] * (0.92 + Math.random() * 0.16)).toFixed(2);
      timings[keys[s.id]] = el2;
      yield { type: "stage", id: s.id, status: "done", elapsed: el2, facts: F[s.id], data: s.id === "patch" ? { tiers, cost } : undefined };
    }
    timings.total = +Object.values(timings).reduce((a, b) => a + b, 0).toFixed(2);
    yield {
      type: "result", data: {
        ws, input_type: repo ? "github" : "text", package: "pydantic", from_version: "1.10.12", to_version: "2.14.1",
        verdict: "PASS", reasons: [], cve_count: 1,
        vulnerabilities: [{ id: "GHSA-mr82-8j83-vxmv", summary: "Regular-expression DoS via crafted email strings in pydantic < 1.10.13" }],
        summary: { vulnerabilities_resolved: 1, vulnerabilities_total: 1, pass_rate_before: 100, pass_rate_after: 100, output_parity_pct: 100, test_time_speedup_x: repo ? 5.28 : null, ai_spend_usd: repo ? 0.1219 : 0.0081, saved_usd: repo ? 0.2016 : 0.0627, files_refactored: files.length },
        tests: { before: { passed: tests[1], collected: tests[0] }, after: { passed: tests[0], collected: tests[0], ok: true }, newly_broken: tests[0] },
        plan: { steps: plan.map(([file, title], i) => ({ step: i + 1, file, title })), mode: "llm", notes: [] },
        patch: { status: "verified", steps_applied: `${plan.length}/${plan.length}`, escalated: true, tiers, cost },
        breakage_symbols: ["validator", "root_validator", "Config", "__root__", "parse_obj", "parse_obj_as", "dict", "json", "copy", "schema", "__fields__", "regex", "max_items"],
        timings_s: timings, files, report_url: "assets/demo-report.pdf",
      },
    };
  }

  /* ---------------------------------------------------------------- landing wiring */
  function goHome() {
    if (state.running) return toast("A run is in progress — it will finish in the background.");
    $$(".sheet.open, .drawer.open, .modal.open").forEach(closeOverlay);
    const cb = $("#continueBtn"); cb.hidden = true; cb.classList.remove("on");
    showView("landing"); setTimeout(() => state.landing.focus(), 600);
  }

  function init() {
    state.landing = createComposer($("#landingComposer"), { onSubmit: startRun });
    state.chat = createComposer($("#chatComposer"), { compact: true, onSubmit: onChatSubmit });
    state.chat.ta.placeholder = "Ask about this run, or paste another repo / snippet…";
    buildFaq(); initGitHub(); detectMode(); startSparks();
    $("#brandHome").onclick = goHome;
    $("#newRunBtn").onclick = goHome;
    $$("[data-sample]").forEach((b) => b.addEventListener("click", () => {
      const k = b.dataset.sample;
      if (k === "snippet") state.landing.setText(V1_SAMPLE.trim());
      else if (k === "repo") openGitHub(state.landing);
      else state.landing.pickFile();
    }));
    // drag & drop a .py anywhere on the landing page
    const veil = $("#dropVeil"); let depth = 0;
    addEventListener("dragenter", (e) => { if (currentView !== "landing" || !e.dataTransfer?.types?.includes("Files")) return; depth++; veil.classList.add("on"); });
    addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; veil.classList.remove("on"); } });
    addEventListener("dragover", (e) => { if (currentView === "landing") e.preventDefault(); });
    addEventListener("drop", (e) => { if (currentView !== "landing") return; e.preventDefault(); depth = 0; veil.classList.remove("on"); const f = e.dataTransfer.files[0]; if (f) state.landing.setFile(f); });
    setTimeout(() => state.landing.focus(), 900);
  }

  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", init) : init();
})();

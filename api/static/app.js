/* PixelForge Cloud dashboard - plain JavaScript, no build step.
   Every number on screen comes from GET /api/dashboard (Redis + Kubernetes state). Nothing is simulated. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let m = r.statusText;
    try { m = (await r.json()).detail || m; } catch (_) {}
    throw new Error(m);
  }
  return r.json();
}
function toast(msg, err) {
  const t = $('toast');
  t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3800);
}
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const fmtDur = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return (h ? h + 'h ' : '') + (h || m ? m + 'm ' : '') + (s % 60) + 's';
};

/* ------------------------------------------------------------------ navigation */
let view = 'dashboard';
function showView(v) {
  view = v;
  document.querySelectorAll('.view').forEach((e) => e.classList.toggle('active', e.id === 'view-' + v));
  document.querySelectorAll('.tab').forEach((e) => e.classList.toggle('active', e.dataset.view === v));
  location.hash = v;
  if (v === 'dashboard') loadTimeline();
  if (v === 'jobs') { loadBatchSelects().then(loadJobs); }
  if (v === 'results') { loadBatchSelects().then(loadResults); }
}
document.querySelectorAll('.tab').forEach((b) => (b.onclick = () => showView(b.dataset.view)));

/* ------------------------------------------------------------------ dashboard */
let lastD = null;
const rateSamples = [];
let rate = 0;

async function tick() {
  try {
    const d = await api('/api/dashboard');
    lastD = d;
    setConn(true);
    renderDash(d);
  } catch (e) { setConn(false); }
  setTimeout(tick, 1000);
}

function setConn(ok) {
  const p = $('conn-pill');
  p.className = 'pill ' + (ok ? 'ok' : 'bad');
  p.lastElementChild.textContent = ok ? 'API live' : 'API unreachable';
}

function renderDash(d) {
  const t = d.totals, k = d.kubernetes;

  // throughput over a rolling ~15 s window
  rateSamples.push({ t: d.now, c: t.completed });
  while (rateSamples.length > 2 && d.now - rateSamples[0].t > 15) rateSamples.shift();
  const a = rateSamples[0], b = rateSamples[rateSamples.length - 1];
  rate = b.t > a.t ? Math.max(0, (b.c - a.c) / (b.t - a.t)) : 0;

  // pipeline
  $('pl-submitted').textContent = t.submitted;
  $('pl-queued').textContent = t.queued;
  $('pl-workers').textContent = t.active_workers;
  $('pl-processing').textContent = t.processing;
  $('pl-completed').textContent = t.completed;
  $('pl-rate').textContent = rate.toFixed(1);
  $('flow1').classList.toggle('on', t.queued > 0);
  $('flow2').classList.toggle('on', t.processing > 0);
  $('flow3').classList.toggle('on', rate > 0.05);
  $('st-queue').classList.toggle('active', t.queued > 0);
  $('st-workers').classList.toggle('active', t.processing > 0);
  $('st-upload').classList.toggle('active', t.queued > 0 && t.processing === 0);

  // stats tiles
  $('s-total').textContent = t.submitted; $('s-queued').textContent = t.queued;
  $('s-processing').textContent = t.processing; $('s-completed').textContent = t.completed;
  $('s-failed').textContent = t.failed; $('s-workers').textContent = t.active_workers;

  renderBatch(d);
  renderWorkers(d);
  renderQueue(d);
  renderK8s(d);
  renderFeed(d);
  renderSelfHeal(d);
}

function renderBatch(d) {
  const b = d.batch;
  if (!b) {
    $('batch-title').textContent = 'No workload yet';
    $('batch-fill').style.width = '0%'; $('batch-pct').textContent = '0%';
    $('batch-done').textContent = 0; $('batch-total').textContent = 0; $('batch-eta').textContent = '';
    $('batch-tag').textContent = 'idle'; $('batch-tag').className = 'tag';
    $('batch-actions').hidden = true; return;
  }
  const finished = b.completed + b.failed;
  $('batch-title').textContent = b.done ? 'Dataset Processed' : 'Processing Dataset';
  $('batch-fill').style.width = b.percent + '%';
  $('batch-fill').classList.toggle('idle', b.done);
  $('batch-pct').textContent = Math.floor(b.percent) + '%';
  $('batch-done').textContent = b.completed; $('batch-total').textContent = b.total;
  const remaining = b.total - finished;
  $('batch-eta').textContent = !b.done && rate > 0.05 ? `· ${rate.toFixed(1)} jobs/s · ETA ~${fmtDur(remaining / rate)}` : (b.label ? '· ' + b.label : '');
  $('batch-tag').textContent = b.done ? 'complete' : 'running';
  $('batch-tag').className = 'tag ' + (b.done ? 'live' : 'warn');
  $('batch-actions').hidden = b.completed === 0;
  $('batch-download').href = `/api/batches/${b.id}/download`;
  $('batch-view').onclick = () => { showView('results'); $('r-batch').value = b.id; loadResults(); };
}

/* ---- worker cards (keyed DOM so progress bars animate smoothly) */
const wEls = new Map();
const CARD_HTML = `<div class="wc-h"><span class="wc-name"></span><span class="wc-st"></span></div>
<div class="wc-pod"></div><div class="wc-job"></div>
<div class="wc-bar"><i></i></div>
<div class="wc-pct"><span class="stage"></span><b class="pct"></b></div>
<div class="wc-foot"><div><b class="done">0</b>completed</div><div><b class="avg">–</b>avg time</div><div><b class="up">–</b>uptime</div></div>`;

function scaledDown(d, stopT) {
  return (d.scale_history || []).some((s) => s.to < s.from && Math.abs(s.t - stopT) < 45);
}

function renderWorkers(d) {
  const k = d.kubernetes;
  const jobById = {};
  d.queue.view.forEach((j) => (jobById[j.id] = j));
  const cards = d.workers.map((w) => {
    let cls, label;
    const podInfo = k.available ? (k.pods || []).find((p) => p.name === w.pod) : null;
    if (w.status === 'PROCESSING') { cls = 'busy'; label = 'PROCESSING'; }
    else if (w.status === 'IDLE') { cls = 'idle'; label = 'IDLE'; }
    else if (w.status === 'LOST') { cls = 'dead'; label = 'FAILED'; }
    else if (scaledDown(d, d.now - w.age)) { cls = 'scaled'; label = 'SCALED DOWN'; }
    else { cls = 'dead'; label = 'TERMINATED'; }
    if (w.online && podInfo && podInfo.terminating) { cls = 'dead'; label = 'TERMINATING'; }
    return { key: w.pod, w, cls, label };
  });
  if (k.available) {
    const known = new Set(d.workers.map((w) => w.pod));
    (k.pods || []).forEach((p) => {
      if (!known.has(p.name) && !p.terminating) cards.push({ key: p.name, w: null, pod: p, cls: 'starting', label: 'STARTING' });
    });
  }
  const box = $('workers');
  const online = d.workers.filter((w) => w.online).length;
  $('workers-sub').textContent = `${online} active · ${d.workers.filter((w) => w.status === 'PROCESSING').length} busy`;
  box.querySelectorAll('.empty').forEach((e) => e.remove());
  if (!cards.length) box.innerHTML = '<div class="empty">Waiting for workers to register…</div>';

  const keys = new Set(cards.map((c) => c.key));
  for (const [key, el] of wEls) if (!keys.has(key)) { el.remove(); wEls.delete(key); }
  cards.sort((a, b) => (a.w ? a.w.ordinal : 1e9) - (b.w ? b.w.ordinal : 1e9));
  cards.forEach((c, i) => {
    let el = wEls.get(c.key);
    if (!el) {
      el = document.createElement('div'); el.innerHTML = CARD_HTML; wEls.set(c.key, el);
    }
    const q = (s) => el.querySelector(s);
    const w = c.w;
    const fresh = w && w.online && w.uptime < 20 && c.cls !== 'dead';
    el.className = `wcard ${c.cls}${fresh ? ' new' : ''}`;
    q('.wc-name').textContent = w ? w.name : 'New pod';
    const st = q('.wc-st'); st.textContent = c.label;
    st.className = 'wc-st st-' + ({ busy: 'busy', idle: 'idle', dead: 'dead', starting: 'starting', scaled: 'scaled' }[c.cls]);
    q('.wc-pod').textContent = w ? w.pod : c.pod.name;
    if (w && w.status === 'PROCESSING') {
      const j = jobById[w.job];
      q('.wc-job').innerHTML = `<b>Job ${esc(w.job)}</b> · ${esc(w.op)}<small>${esc(j ? j.original_name : '')} · ${esc(w.stage)}</small>`;
    } else if (c.cls === 'dead') {
      q('.wc-job').innerHTML = '<b>Pod gone</b><small>Kubernetes replaces it automatically</small>';
    } else if (c.cls === 'starting') {
      q('.wc-job').innerHTML = '<b>Pod starting…</b><small>container being created</small>';
    } else {
      q('.wc-job').innerHTML = `<b>Waiting for a job</b><small>${w && w.last_ms ? 'last job ' + w.last_ms + ' ms' : 'idle'}</small>`;
    }
    const pct = w && w.status === 'PROCESSING' ? w.progress : (w && w.online ? 0 : 0);
    q('.wc-bar i').style.width = pct + '%';
    q('.stage').textContent = w && w.status === 'PROCESSING' ? w.stage : '';
    q('.pct').textContent = w && w.status === 'PROCESSING' ? pct + '%' : '';
    q('.done').textContent = w ? w.completed : 0;
    q('.avg').textContent = w && w.avg_ms ? w.avg_ms + ' ms' : '–';
    q('.up').textContent = w ? fmtDur(w.uptime) : '–';
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
  });
}

/* ---- queue list */
const seenQ = new Set();
function renderQueue(d) {
  const q = d.queue;
  $('q-len').textContent = q.length;
  const box = $('qlist');
  if (!q.view.length) { box.innerHTML = '<div class="empty">Queue is empty</div>'; return; }
  const waiting = q.view.filter((j) => j.status === 'QUEUED').length;
  const workerOf = (j) => j.worker || '';
  box.innerHTML = q.view.map((j) => {
    const key = j.id + j.status, fresh = !seenQ.has(key); seenQ.add(key);
    const p = j.status === 'PROCESSING';
    return `<div class="qi ${p ? 'p' : ''}" style="${fresh ? '' : 'animation:none'}"><span class="jid">[Job ${j.id}]</span>
      <span class="jst">${j.status}${p ? ' → ' + esc(workerOf(j)) : ''}</span>
      <span class="jop">${esc(j.operation)}</span><span class="jw">${p ? j.progress + '%' : ''}</span></div>`;
  }).join('') + (q.length > waiting ? `<div class="qmore">+ ${q.length - waiting} more jobs waiting in Redis</div>` : '');
}

/* ---- Kubernetes */
function renderK8s(d) {
  const k = d.kubernetes, hpa = k.hpa || {};
  const kp = $('k8s-pill');
  kp.className = 'pill ' + (k.available ? 'ok' : '');
  kp.lastElementChild.textContent = k.available ? `K8s · ${k.desired} replicas` : 'K8s API: off';
  const online = d.workers.filter((w) => w.online);
  $('k-cur').textContent = k.available ? (k.current ?? k.desired) : online.length;
  $('k-min').textContent = hpa.min ?? '–';
  $('k-max').textContent = hpa.max ?? '–';
  $('k-des').textContent = k.available ? k.desired : '–';
  $('hpa-tag').textContent = k.available ? 'queue depth' : 'not in a cluster';
  $('hpa-tag').className = 'tag ' + (k.available ? 'live' : 'warn');

  // pod tiles reflect real pods
  const byPod = {}; d.workers.forEach((w) => (byPod[w.pod] = w));
  let tiles;
  if (k.available) {
    tiles = (k.pods || []).map((p) => {
      const w = byPod[p.name];
      const c = p.terminating ? 'term' : (!p.ready || !w) ? 'start' : (w.status === 'PROCESSING' ? 'busy' : '');
      return `<div class="pt ${c}" title="${esc(p.name)} · ${esc(p.phase)}">${w ? w.ordinal : '…'}</div>`;
    });
  } else tiles = online.map((w) => `<div class="pt ${w.status === 'PROCESSING' ? 'busy' : ''}" title="${esc(w.pod)}">${w.ordinal}</div>`);
  const th = tiles.join('');
  if ($('podtiles').dataset.h !== th) { $('podtiles').innerHTML = th; $('podtiles').dataset.h = th; }

  // CPU gauge (target marker fixed at 50% of the bar => bar spans 0..2x target)
  const cpu = hpa.cpu_current, tgt = hpa.cpu_target;
  $('cpu-val').textContent = cpu != null ? cpu + '%' : (k.available ? 'metrics pending…' : 'n/a');
  $('cpu-tgt').textContent = tgt != null ? tgt + '%' : '–';
  $('cpu-fill').style.width = cpu != null && tgt ? Math.min(100, (cpu / (2 * tgt)) * 100) + '%' : '0%';

  // scaling narrative from the REAL replica history
  const hist = (d.scale_history || []).filter((s) => d.now - s.t < 240).reverse();
  const msg = $('scalemsg'), chain = $('scalechain');
  if (!k.available) {
    msg.className = 'scalemsg'; msg.textContent = 'Autoscaling unavailable: Docker Compose uses 3 fixed workers. Deploy with Kubernetes for queue-based autoscaling.'; chain.innerHTML = '';
  } else if (hist.length) {
    const last = hist[hist.length - 1];
    const seq = [hist[0].from, ...hist.map((s) => s.to)];
    chain.innerHTML = seq.map((n, i) => `<span class="${i === seq.length - 1 ? 'last' : ''}">${n}</span>`).join('<span class="ar">→</span>');
    if (last.to > last.from) { msg.className = 'scalemsg up'; msg.textContent = `Queue: ${d.totals.queued} jobs — scaling workers up…`; }
    else { msg.className = 'scalemsg down'; msg.textContent = 'Workload normalized — scaling workers down'; }
  } else {
    chain.innerHTML = '';
    msg.className = 'scalemsg';
    msg.textContent = d.totals.queued > 0 ? `Queue: ${d.totals.queued} jobs — workers scale from queued + active work` : `Steady — ${k.desired} workers`;
  }
}

/* ---- activity feed */
let feedKey = '';
const seenEv = new Set();
function renderFeed(d) {
  const key = d.events.length + (d.events[0] ? d.events[0].t : '');
  if (key === feedKey) return;
  feedKey = key;
  $('feed').innerHTML = d.events.slice(0, 30).map((e) => {
    const id = e.t + e.msg, fresh = !seenEv.has(id); seenEv.add(id);
    return `<li class="k-${e.kind}" style="${fresh ? '' : 'animation:none'}"><time>${fmtTime(e.t)}</time><span>${esc(e.msg)}</span></li>`;
  }).join('') || '<li><span class="muted">No activity yet — start a workload.</span></li>';
}

/* ---- self-healing banner (derived from real worker events, scale-downs excluded) */
function renderSelfHeal(d) {
  const box = $('selfheal');
  const ev = d.events;
  const lost = ev.find((e) => (e.kind === 'worker_lost' || e.kind === 'worker_stopped') && d.now - e.t < 90 && !scaledDown(d, e.t));
  if (!lost) { box.hidden = true; return; }
  const online = ev.find((e) => e.kind === 'worker_online' && e.t > lost.t);
  if (online) {
    if (d.now - online.t > 25) { box.hidden = true; return; }
    box.className = 'selfheal good';
    box.innerHTML = `<div><span class="arrow">${esc(lost.worker)} FAILED&nbsp; → &nbsp;${esc(online.worker)} ONLINE</span><small>Pod recreated by the Kubernetes Deployment · remaining jobs continue</small></div>`;
  } else {
    box.className = 'selfheal bad';
    box.innerHTML = `<div class="spinner"></div><div>${esc(lost.worker)} — FAILED<small>Kubernetes is recreating the worker…</small></div>`;
  }
  box.hidden = false;
}

/* ---- timeline chart */
async function loadTimeline() {
  if (view !== 'dashboard') return;
  try { drawChart(await api('/api/timeline?n=150')); } catch (_) {}
  clearTimeout(loadTimeline.h); loadTimeline.h = setTimeout(loadTimeline, 3000);
}
function drawChart(data) {
  const cv = $('chart'), dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = 190;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  const pad = { l: 36, r: 10, t: 10, b: 20 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  g.font = '10px Inter,sans-serif'; g.fillStyle = '#8b96bd'; g.strokeStyle = 'rgba(255,255,255,.07)';
  if (data.length < 2) { g.fillText('Collecting samples…', pad.l, H / 2); return; }
  const mx = Math.max(5, ...data.map((s) => Math.max(s.queue, s.processing, s.replicas || 0)));
  const x = (i) => pad.l + (i / (data.length - 1)) * iw;
  const y = (v) => pad.t + ih - (v / mx) * ih;
  for (let i = 0; i <= 4; i++) {
    const yy = pad.t + (ih * i) / 4; g.beginPath(); g.moveTo(pad.l, yy); g.lineTo(W - pad.r, yy); g.stroke();
    g.fillText(Math.round(mx * (1 - i / 4)), 4, yy + 3);
  }
  const line = (key, color, fill, step, scale) => {
    g.beginPath();
    data.forEach((s, i) => {
      const v = (s[key] ?? 0) * (scale || 1);
      if (i === 0) g.moveTo(x(i), y(v)); else { if (step) g.lineTo(x(i), y((data[i - 1][key] ?? 0) * (scale || 1))); g.lineTo(x(i), y(v)); }
    });
    g.strokeStyle = color; g.lineWidth = 2; g.stroke();
    if (fill) { g.lineTo(x(data.length - 1), y(0)); g.lineTo(x(0), y(0)); g.closePath(); g.fillStyle = fill; g.fill(); }
  };
  line('queue', '#22d3ee', 'rgba(34,211,238,.12)');
  line('processing', '#fbbf24');
  line('replicas', '#a78bfa', null, true);
  if (data.some((s) => s.cpu != null)) line('cpu', '#34d399', null, false, mx / Math.max(100, ...data.map((s) => s.cpu || 0)));
  g.fillStyle = '#8b96bd';
  g.fillText(fmtTime(data[0].t), pad.l, H - 5);
  g.textAlign = 'right'; g.fillText(fmtTime(data[data.length - 1].t), W - pad.r, H - 5); g.textAlign = 'left';
}

/* ---- demo workload launcher */
function demoEq() {
  const op = $('d-op').value, orig = +$('d-orig').value || 1;
  const aug = op === 'augment';
  $('d-per-wrap').hidden = !aug; $('d-count-wrap').hidden = aug;
  $('d-int-wrap').hidden = !['blur', 'sharpen'].includes(op);
  const per = +$('d-per').value || 1, cnt = +$('d-count').value || 1;
  $('d-eq').textContent = aug ? `${orig} original images × ${per} augmentations = ${orig * per} jobs` : `${cnt} jobs · cycling over ${orig} source images`;
}
['d-op', 'd-orig', 'd-per', 'd-count', 'd-int'].forEach((id) => ($(id).oninput = demoEq));
demoEq();
$('d-start').onclick = async () => {
  const op = $('d-op').value;
  const params = { augment: { per_image: +$('d-per').value, augment_ops: ['rotate', 'flip_h', 'flip_v', 'crop', 'brightness', 'contrast', 'noise'] },
    resize: { scale: 50 }, blur: { radius: 6, passes: +$('d-int').value }, compress: { quality: 50 }, convert: { format: 'webp' }, grayscale: {}, sharpen: { amount: 2.5, passes: +$('d-int').value } }[op];
  $('d-start').disabled = true; $('d-start').textContent = 'Generating images…';
  try {
    const r = await api('/api/demo', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation: op, count: +$('d-count').value, originals: +$('d-orig').value, size: +$('d-size').value, params }) });
    toast(`${r.jobs} jobs pushed into the Redis queue`);
  } catch (e) { toast(e.message, true); }
  $('d-start').disabled = false; $('d-start').textContent = '🚀 Start Processing';
};
$('d-reset').onclick = async () => {
  if (!confirm('Clear queue, jobs, events and stored files?')) return;
  try { await api('/api/reset', { method: 'POST' }); rateSamples.length = 0; seenQ.clear(); seenEv.clear(); toast('System reset'); } catch (e) { toast(e.message, true); }
};

/* ------------------------------------------------------------------ process page */
const OPS = {
  resize: { icon: '⤢', name: 'Resize', fields: [['scale', 'Scale (%)', 50], ['width', 'or Width (px)', ''], ['height', 'or Height (px)', '']] },
  crop: { icon: '✂', name: 'Crop', fields: [['percent', 'Centre crop (%)', 80]] },
  rotate: { icon: '⟳', name: 'Rotate', fields: [['angle', 'Angle (°)', 90]] },
  flip_h: { icon: '⇋', name: 'Flip H', fields: [] },
  flip_v: { icon: '⇅', name: 'Flip V', fields: [] },
  convert: { icon: '⇄', name: 'Convert', fields: [['format', 'Target format', 'webp', ['jpeg', 'png', 'webp']], ['quality', 'Quality', 90]] },
  compress: { icon: '🗜', name: 'Compress', fields: [['quality', 'Quality (1-95)', 55]] },
  thumbnail: { icon: '▣', name: 'Thumbnail', fields: [['size', 'Max size (px)', 256]] },
  grayscale: { icon: '◐', name: 'Grayscale', fields: [] },
  brightness: { icon: '☀', name: 'Brightness', fields: [['factor', 'Factor (1 = same)', 1.3]] },
  contrast: { icon: '◑', name: 'Contrast', fields: [['factor', 'Factor (1 = same)', 1.3]] },
  sharpen: { icon: '◇', name: 'Sharpen', fields: [['amount', 'Amount', 2], ['passes', 'Passes', 1]] },
  blur: { icon: '◌', name: 'Blur', fields: [['radius', 'Radius', 4], ['passes', 'Passes', 1]] },
  augment: { icon: '✦', name: 'Dataset Augmentation', fields: [['per_image', 'Augmentations per image', 5], ['max_angle', 'Max rotation (±°)', 20]], augment: true },
};
const AUG = ['rotate', 'flip_h', 'flip_v', 'crop', 'brightness', 'contrast', 'noise'];
let selOp = 'resize';
let picked = []; // {file, path}

function buildOps() {
  $('opgrid').innerHTML = Object.entries(OPS).map(([k, o]) => `<button class="op ${k === selOp ? 'sel' : ''}" data-op="${k}" id="op-${k}"><em>${o.icon}</em>${o.name}</button>`).join('');
  $('opgrid').querySelectorAll('.op').forEach((b) => (b.onclick = () => { selOp = b.dataset.op; buildOps(); buildParams(); }));
}
function buildParams() {
  const o = OPS[selOp];
  let h = o.fields.map(([k, label, def, opts]) => opts
    ? `<label>${label}<select data-p="${k}">${opts.map((x) => `<option ${x === def ? 'selected' : ''}>${x}</option>`).join('')}</select></label>`
    : `<label>${label}<input data-p="${k}" type="number" step="any" value="${def}"></label>`).join('');
  if (o.augment) h += `<div class="full"><label>Augmentations to apply</label><div class="checks">${AUG.map((a) => `<label><input type="checkbox" data-aug="${a}" checked>${a.replace('_', ' ')}</label>`).join('')}</div></div>`;
  $('params').innerHTML = h || '<p class="muted full">No parameters for this operation.</p>';
  $('params').querySelectorAll('input,select').forEach((i) => (i.oninput = procEq));
  procEq();
}
function collectParams() {
  const p = {};
  $('params').querySelectorAll('[data-p]').forEach((i) => { if (i.value !== '') p[i.dataset.p] = i.tagName === 'SELECT' ? i.value : +i.value; });
  if (OPS[selOp].augment) p.augment_ops = [...$('params').querySelectorAll('[data-aug]')].filter((c) => c.checked).map((c) => c.dataset.aug);
  return p;
}
function procEq() {
  const n = picked.length, p = collectParams();
  $('p-submit').disabled = n === 0;
  if (!n) { $('p-eq').textContent = 'Select images to begin'; return; }
  if (datasetMode) {
    const classes = new Set(picked.map((x) => { const pts = x.path.split('/'); return pts.length >= 2 ? pts[pts.length - 2] : null; }).filter(Boolean));
    const jobs = selOp === 'augment' ? n * (p.per_image || 5) : n;
    $('p-eq').textContent = selOp === 'augment'
      ? `${n} images × ${p.per_image || 5} = ${jobs} jobs · ${classes.size} classes · results → class folders`
      : `${n} images → ${jobs} jobs · ${classes.size} classes · results → class folders`;
  } else {
    $('p-eq').textContent = selOp === 'augment'
      ? `${n} images × ${p.per_image || 5} = ${n * (p.per_image || 5)} jobs`
      : `${n} image${n > 1 ? 's' : ''} → ${n} job${n > 1 ? 's' : ''}`;
  }
}
let datasetMode = false; // true when files come from a multi-class folder

function setFiles(list) {
  picked = list.filter((x) => /\.(jpe?g|png|webp)$/i.test(x.file.name));
  const skipped = list.length - picked.length;
  if (skipped) toast(`${skipped} non-image file(s) skipped`, true);

  // detect dataset mode: folder upload where images sit inside class sub-folders
  const topDirs = new Set(picked.map((x) => {
    const parts = x.path.split('/');
    return parts.length >= 2 ? parts[parts.length - 2] : null; // immediate parent dir
  }).filter(Boolean));
  datasetMode = topDirs.size >= 2; // at least 2 distinct class folders

  renderFileList();
  procEq();
}

function renderFileList() {
  const box = $('filelist');
  box.innerHTML = '';

  // dataset mode banner
  const oldBanner = document.getElementById('dataset-banner-el');
  if (oldBanner) oldBanner.remove();
  if (datasetMode) {
    const classes = new Set(picked.map((x) => {
      const parts = x.path.split('/');
      return parts.length >= 2 ? parts[parts.length - 2] : null;
    }).filter(Boolean));
    const banner = document.createElement('div');
    banner.className = 'dataset-banner'; banner.id = 'dataset-banner-el';
    banner.innerHTML = `<em>📂</em><span>Dataset mode · ${classes.size} classes detected<small>${[...classes].slice(0,6).join(' · ')}${classes.size > 6 ? ` +${classes.size - 6} more` : ''}</small></span>`;
    box.before(banner);
  }

  picked.slice(0, 60).forEach((x, idx) => {
    const d = document.createElement('div');
    d.className = 'fthumb';
    d.style.backgroundImage = `url(${URL.createObjectURL(x.file)})`;

    // class-folder badge (only in dataset mode)
    if (datasetMode) {
      const parts = x.path.split('/');
      const cls = parts.length >= 2 ? parts[parts.length - 2] : null;
      if (cls) {
        const badge = document.createElement('div');
        badge.className = 'fthumb-class';
        badge.textContent = cls;
        d.appendChild(badge);
      }
    }

    // filename label
    const lbl = document.createElement('span');
    lbl.textContent = x.path;
    d.appendChild(lbl);

    // ✕ remove button
    const rm = document.createElement('button');
    rm.className = 'fthumb-rm';
    rm.setAttribute('aria-label', 'Remove');
    rm.innerHTML = '✕';
    rm.onclick = (e) => {
      e.stopPropagation();
      // find real index in picked (not sliced index)
      const realIdx = picked.indexOf(x);
      if (realIdx !== -1) {
        picked.splice(realIdx, 1);
        renderFileList();
        procEq();
      }
    };
    d.appendChild(rm);
    box.appendChild(d);
  });

  if (picked.length > 60) {
    box.insertAdjacentHTML('beforeend', `<div class="muted" style="grid-column:1/-1;text-align:center;padding:6px">+${picked.length - 60} more (scroll to see all · hover any to remove)</div>`);
  } else if (picked.length === 0) {
    box.innerHTML = '';
  }
}
$('pick-files').onclick = () => $('file-input').click();
$('pick-folder').onclick = () => $('folder-input').click();
$('file-input').onchange = (e) => setFiles([...e.target.files].map((f) => ({ file: f, path: f.name })));
$('folder-input').onchange = (e) => setFiles([...e.target.files].map((f) => ({ file: f, path: f.webkitRelativePath || f.name })));
const drop = $('drop');
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', async (e) => {
  const items = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  const out = [];
  const walk = async (en, base) => {
    if (en.isFile) { const f = await new Promise((r) => en.file(r)); out.push({ file: f, path: base + f.name }); }
    else if (en.isDirectory) {
      const rd = en.createReader();
      let batch;
      do { batch = await new Promise((r) => rd.readEntries(r)); for (const c of batch) await walk(c, base + en.name + '/'); } while (batch.length);
    }
  };
  for (const en of items) await walk(en, '');
  setFiles(out.length ? out : [...e.dataTransfer.files].map((f) => ({ file: f, path: f.name })));
});

let trackTimer = null;
$('p-submit').onclick = async () => {
  const fd = new FormData();
  picked.forEach((x) => fd.append('files', x.file, x.file.name));
  fd.append('paths', JSON.stringify(picked.map((x) => x.path)));
  fd.append('operation', selOp); fd.append('params', JSON.stringify(collectParams()));
  $('p-submit').disabled = true; $('p-submit').textContent = 'Uploading…';
  try {
    const r = await api('/api/process', { method: 'POST', body: fd });
    toast(`Batch ${r.batch_id}: ${r.jobs} job(s) queued`);
    trackBatch(r.batch_id);
  } catch (e) { toast(e.message, true); }
  $('p-submit').textContent = 'Process images'; procEq();
};
function trackBatch(id) {
  $('track-panel').hidden = false;
  clearTimeout(trackTimer);
  const poll = async () => {
    try {
      const [b, j] = await Promise.all([api('/api/batches/' + id), api(`/api/jobs?batch=${id}&limit=60`)]);
      $('track-fill').style.width = b.percent + '%'; $('track-pct').textContent = `${Math.floor(b.percent)}% · ${b.completed}/${b.total}`;
      $('track-tag').textContent = b.done ? 'complete' : 'processing'; $('track-tag').className = 'tag ' + (b.done ? 'live' : 'warn');
      $('track-cards').innerHTML = j.jobs.slice().reverse().map(jobCard).join('');
      $('track-cards').querySelectorAll('.jcard').forEach((c) => (c.onclick = () => openJob(+c.dataset.id)));
      if (!b.done) trackTimer = setTimeout(poll, 800);
    } catch (_) { trackTimer = setTimeout(poll, 1500); }
  };
  poll();
}
function jobCard(j) {
  const done = j.status === 'COMPLETED';
  return `<div class="jcard" data-id="${j.id}"><div class="im" style="${done ? `background-image:url(/api/jobs/${j.id}/thumbnail)` : ''}">${done ? '' : j.status === 'FAILED' ? '⚠ failed' : j.status === 'PROCESSING' ? j.progress + '% · ' + esc(j.worker) : 'queued'}</div>
  <div class="bd"><b>${esc(j.original_name)}</b><span class="badge b-${j.status}">${j.status}</span> <span class="muted">${j.duration_ms ? j.duration_ms + ' ms' : ''} ${done ? '· ' + esc(j.worker) : ''}</span></div></div>`;
}

/* lightbox */
async function openJob(id) {
  try {
    const j = await api('/api/jobs/' + id);
    $('lb-title').textContent = `Job ${j.id} · ${j.operation} · ${j.original_name}`;
    $('lb-in').src = `/api/jobs/${id}/input`;
    $('lb-out').src = j.status === 'COMPLETED' ? `/api/jobs/${id}/result` : '';
    $('lb-meta').innerHTML = `<span>status: ${j.status}</span><span>worker: ${esc(j.worker || '–')}</span><span>time: ${j.duration_ms ?? '–'} ms</span>${j.error ? `<span style="color:var(--rd)">${esc(j.error)}</span>` : ''}`;
    $('lb-dl').style.display = j.status === 'COMPLETED' ? '' : 'none';
    $('lb-dl').href = `/api/jobs/${id}/result?download=true`;
    $('lightbox').hidden = false;
  } catch (e) { toast(e.message, true); }
}
$('lb-x').onclick = () => ($('lightbox').hidden = true);
$('lightbox').onclick = (e) => { if (e.target === $('lightbox')) $('lightbox').hidden = true; };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('lightbox').hidden = true; });

/* ------------------------------------------------------------------ jobs + results pages */
async function loadBatchSelects() {
  try {
    const bs = await api('/api/batches');
    const jb = $('j-batch'), rb = $('r-batch'), cj = jb.value, cr = rb.value;
    jb.innerHTML = '<option value="">All batches</option>' + bs.map((b) => `<option value="${b.id}">${b.id} · ${esc(b.label || b.operation)} (${b.total})</option>`).join('');
    rb.innerHTML = bs.length ? bs.map((b) => `<option value="${b.id}">${b.id} · ${esc(b.label || b.operation)} (${b.completed}/${b.total})</option>`).join('') : '<option value="">No batches</option>';
    jb.value = cj; if (cr) rb.value = cr;
  } catch (_) {}
}
async function loadJobs() {
  if (view !== 'jobs') return;
  try {
    const q = new URLSearchParams({ limit: 150 });
    if ($('j-batch').value) q.set('batch', $('j-batch').value);
    if ($('j-status').value) q.set('status', $('j-status').value);
    const r = await api('/api/jobs?' + q);
    $('jobs-body').innerHTML = r.jobs.map((j) => `<tr data-id="${j.id}"><td>#${j.id}</td><td>${esc(j.original_name)}</td><td>${esc(j.operation)}</td>
      <td><span class="badge b-${j.status}">${j.status}</span></td><td><div class="mini"><i style="width:${j.progress}%"></i></div></td>
      <td>${esc(j.worker || '–')}</td><td>${j.duration_ms ? j.duration_ms + ' ms' : '–'}</td></tr>`).join('') || '<tr><td colspan="7" class="muted center">No jobs</td></tr>';
    $('jobs-body').querySelectorAll('tr[data-id]').forEach((tr) => (tr.onclick = () => openJob(+tr.dataset.id)));
    $('jobs-count').textContent = `Showing ${r.jobs.length} of ${r.total} jobs`;
  } catch (_) {}
  clearTimeout(loadJobs.h); loadJobs.h = setTimeout(loadJobs, 2000);
}
$('j-batch').onchange = loadJobs; $('j-status').onchange = loadJobs;

async function loadResults() {
  if (view !== 'results') return;
  const b = $('r-batch').value;
  if (!b) { $('gallery').innerHTML = '<div class="empty">No completed results yet.</div>'; return; }
  try {
    const r = await api(`/api/jobs?batch=${b}&status=COMPLETED&limit=300`);
    $('gallery').innerHTML = r.jobs.map((j) => `<div class="gi" data-id="${j.id}" style="background-image:url(/api/jobs/${j.id}/thumbnail)"><span>${esc(j.output_file.split('/').slice(1).join('/'))}</span></div>`).join('') || '<div class="empty">No completed results yet.</div>';
    $('gallery').querySelectorAll('.gi').forEach((g) => (g.onclick = () => openJob(+g.dataset.id)));
    $('r-count').textContent = `${r.total} processed images · structure preserved in the .zip`;
    $('r-zip').href = `/api/batches/${b}/download`;
  } catch (_) {}
  clearTimeout(loadResults.h); loadResults.h = setTimeout(loadResults, 3000);
}
$('r-batch').onchange = loadResults;

/* ------------------------------------------------------------------ boot */
buildOps(); buildParams();
const initial = location.hash.slice(1);
if (['dashboard', 'process', 'jobs', 'results'].includes(initial)) showView(initial); else loadTimeline();
tick();
window.addEventListener('resize', () => view === 'dashboard' && loadTimeline());

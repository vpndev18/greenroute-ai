import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import { getFirestore, collection, query, orderBy, limit, onSnapshot, doc } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js";

const $ = (id) => document.getElementById(id);
// The backend rounds CO2 to 0.1 mg, so smaller values are shown as "<0.1 mg".
const fmtG = (g) => g >= 1000 ? (g / 1000).toFixed(2) + " kg" : g >= 1 ? g.toFixed(2) + " g"
  : g > 0 ? (g * 1000).toFixed(1) + " mg" : "<0.1 mg";
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const TIER_LABEL = { lite: "Flash-Lite", flash: "Flash", pro: "Pro" };
const TIER_COLOR = { lite: "#a3e635", flash: "#34d27b", pro: "#f87171" };
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

// Animate a formatted number ("357.1 mg", "1,204", "96.9%") from its previous value.
function countTo(el, text) {
  const m = /^([−-]?)([\d,]*\.?\d+)(.*)$/.exec(text);
  if (!m || reduceMotion) { el.textContent = text; return; }
  const sign = m[1], target = parseFloat(m[2].replace(/,/g, "")), suffix = m[3];
  const dec = (m[2].split(".")[1] || "").length, from = parseFloat(el.dataset.v || "0");
  el.dataset.v = target;
  const t0 = performance.now(), dur = 1100;
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur), v = from + (target - from) * (1 - Math.pow(1 - k, 3));
    el.textContent = sign + v.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec }) + suffix;
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function setStatus(text, busy = false) {
  $("status").textContent = text;
  $("status").classList.toggle("busy", busy);
}

// ---------- Firebase (realtime ledger + KPIs) ----------
let fs = null;
try {
  fs = getFirestore(initializeApp(window.FIREBASE_CONFIG));
  onSnapshot(doc(fs, "stats", "global"), (s) => {
    const d = s.data() || {};
    countTo($("k-req"), (d.requests || 0).toLocaleString());
    countTo($("k-saved"), fmtG(d.saved_g || 0));
    countTo($("k-pct"), d.baseline_co2_g ? Math.round(100 * d.saved_g / d.baseline_co2_g) + "%" : "–");
    countTo($("k-wh"), (d.energy_wh || 0).toFixed(2) + " Wh");
  });
  onSnapshot(query(collection(fs, "requests"), orderBy("created_at", "desc"), limit(12)), (snap) => {
    const rows = snap.docs.map((d) => d.data());
    if (!rows.length) return;
    $("feed").innerHTML = rows.map((r) => {
      const st = r.status === "done" ? '<span class="tag done">done</span>'
        : r.status === "scheduled" ? '<span class="tag wait">deferred</span>'
        : r.status === "failed" ? '<span class="tag fail">failed</span>'
        : `<span class="tag wait">${esc(r.status)}</span>`;
      return `<tr>
        <td>${esc((r.prompt_preview || "").slice(0, 42))}${r.has_image ? " 🖼️" : ""}</td>
        <td>${r.tier ? `<span class="tier">${esc(TIER_LABEL[r.tier] || r.tier)}</span>` : ""}</td>
        <td>${esc(r.region_name || r.planned_region_name || "")}</td>
        <td>${st}</td>
        <td class="num">${r.co2_g != null ? fmtG(r.co2_g) : "–"}</td>
        <td class="num">${r.saved_pct != null ? `<span class="ok">${r.saved_pct}%</span>` : "–"}</td></tr>`;
    }).join("");
  });
} catch (e) { console.warn("Firebase not configured", e); }

// ---------- Router ----------
document.querySelectorAll("[data-p]").forEach((b) => b.onclick = () => {
  $("prompt").value = b.dataset.p; $("defer").checked = !!b.dataset.defer; $("prompt").focus();
});
$("image").onchange = () => { const f = $("image").files[0]; $("imageName").textContent = f ? f.name : "Attach image"; };

async function fileToB64(f) {
  const buf = await f.arrayBuffer(); let s = ""; const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function traceHtml(p, r) {
  const cell = (k, v) => `<div>${k}<b>${v}</b></div>`;
  return `<div class="trace">
    ${cell("Gemini triage", `${esc(p.complexity)} → ${esc(p.tier_label)}`)}
    ${cell("Predicted output (BigQuery ML)", `${p.vertex_pred_out_tokens} tokens`)}
    ${cell("Region", `${esc(r ? r.region_name : p.planned_region_name)} · ${r ? r.intensity : p.planned_intensity} g/kWh`)}
    ${cell("Baseline grid (Mumbai)", `${p.now_intensity_baseline} g/kWh`)}
    ${cell("When", p.deferred_by_min > 0 ? `+${Math.round(p.deferred_by_min / 6) / 10} h · green window` : "now")}
    ${r ? cell("Model served", `${esc(r.model)} @ ${esc(r.served_location)}`) : ""}
    ${r ? cell("Tokens in / out", `${r.in_tokens} / ${r.out_tokens}`) : ""}
    ${r ? cell("Energy", `${r.energy_wh} Wh`) : cell("Est. energy", `${p.est_energy_wh} Wh`)}
  </div><p class="why">Why this tier: ${esc(p.triage_reason)}</p>`;
}

function saveHtml(r) {
  const pct = Math.max(0, Math.min(100, Number(r.saved_pct) || 0));
  return `<div class="receipt">
    <div class="ring" style="--p:${pct}"><div><b>−${pct}%</b><small>CO₂ saved</small></div></div>
    <div class="receipt-body">
      <div class="receipt-title">Carbon receipt</div>
      <div class="kv"><span>This request</span><b>${fmtG(r.co2_g)}</b></div>
      <div class="kv"><span>Baseline · Pro, Mumbai, now</span><b>${fmtG(r.baseline_co2_g)}</b></div>
      <div class="kv"><span>Latency</span><b>${r.latency_s}s</b></div>
    </div></div>`;
}

const doneHtml = (plan, r) => saveHtml(r) + traceHtml(plan, r) + `<div class="answer">${marked.parse(r.answer || "")}</div>`;

$("go").onclick = async () => {
  const prompt = $("prompt").value.trim(); if (!prompt) { $("prompt").focus(); return; }
  $("go").disabled = true; setStatus("Triaging with Gemini…", true); $("result").innerHTML = "";
  try {
    const body = { prompt, deferrable: $("defer").checked, deadline_hours: +$("deadline").value, residency: $("residency").value };
    const f = $("image").files[0];
    if (f) { body.image_b64 = await fileToB64(f); body.image_mime = f.type; }
    const res = await fetch("/api/route", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    if (data.status === "done") {
      $("result").innerHTML = doneHtml(data.plan, data.result);
      setStatus("");
    } else {
      setStatus("Queued on Pub/Sub · the worker will run it in the green window…", true);
      $("result").innerHTML = traceHtml(data.plan, null) + `<div class="answer"><p>⏳ Deferred job <code>${data.id}</code> queued on Pub/Sub…</p></div>`;
      if (fs) {
        const unsub = onSnapshot(doc(fs, "requests", data.id), (s) => {
          const r = s.data(); if (!r || r.status !== "done") return;
          unsub(); setStatus("Deferred job finished via the Pub/Sub worker.");
          $("result").innerHTML = doneHtml(data.plan, r);
        });
      }
    }
  } catch (e) { setStatus("Error: " + e.message); }
  finally { $("go").disabled = false; }
};

// ---------- Analyst ----------
document.querySelectorAll("[data-q]").forEach((b) => b.onclick = () => { $("q").value = b.dataset.q; $("ask").click(); });
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("ask").click(); });
$("ask").onclick = async () => {
  const question = $("q").value.trim(); if (!question) return;
  $("ask").disabled = true; $("askOut").innerHTML = '<p class="status busy">Thinking…</p>';
  try {
    const res = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) });
    const d = await res.json(); if (!res.ok) throw new Error(d.detail);
    $("askOut").innerHTML = marked.parse(d.answer || "");
  } catch (e) { $("askOut").textContent = "Error: " + e.message; }
  finally { $("ask").disabled = false; }
};

// ---------- Charts ----------
Chart.defaults.font.family = "Inter, system-ui, sans-serif";
Chart.defaults.color = css("--muted"); Chart.defaults.borderColor = css("--line");
Chart.defaults.plugins.tooltip.padding = 10; Chart.defaults.plugins.tooltip.cornerRadius = 8;
const colorFor = (v) => v < 100 ? "#34d27b" : v < 300 ? "#a3e635" : v < 500 ? "#fbbf24" : "#f87171";
const GRID = "rgba(255,255,255,.06)";

fetch("/api/regions").then((r) => r.json()).then((d) => {
  new Chart($("regionChart"), {
    type: "bar",
    data: { labels: d.regions.map((r) => r.location), datasets: [{ data: d.regions.map((r) => r.now), backgroundColor: d.regions.map((r) => colorFor(r.now)), borderRadius: 6, barThickness: 16 }] },
    options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => ` ${c.parsed.x} gCO₂/kWh` } } },
      scales: { x: { grid: { color: GRID }, border: { display: false } }, y: { grid: { display: false }, border: { display: false } } } },
  });
});

fetch("/api/fleet").then((r) => r.json()).then((d) => {
  const s = d.summary;
  if (!s) { $("fleet").textContent = "Fleet replay not loaded yet (run scripts/setup_gcp.sh)."; return; }
  $("fleet").innerHTML = `<div class="fleet-top"><div class="big"><span id="fleetPct">0</span>% CO₂</div>
      <div class="muted">India-only mode still saves <b>${s.saved_pct_india}%</b><br>through right-sizing alone.</div></div>
    <div class="stats">
      <div><small>Requests replayed</small><b>${(Number(s.requests) / 1e6).toFixed(1)}M</b></div>
      <div><small>Baseline</small><b>${Math.round(s.baseline_kg).toLocaleString()} kg</b></div>
      <div><small>GreenRoute</small><b>${Math.round(s.greenroute_kg).toLocaleString()} kg</b></div>
    </div>`;
  countTo($("fleetPct"), "−" + s.saved_pct_global);
  const tiers = [...d.tiers].sort((a, b) => ["lite", "flash", "pro"].indexOf(a.tier) - ["lite", "flash", "pro"].indexOf(b.tier));
  new Chart($("tierChart"), {
    type: "doughnut",
    data: { labels: tiers.map((t) => `${TIER_LABEL[t.tier] || t.tier} · ${t.pct}%`), datasets: [{ data: tiers.map((t) => t.requests), backgroundColor: tiers.map((t) => TIER_COLOR[t.tier] || "#999"), borderColor: css("--card-solid"), borderWidth: 3, hoverOffset: 6 }] },
    options: { maintainAspectRatio: false, cutout: "62%", plugins: { legend: { position: "right", labels: { usePointStyle: true, pointStyle: "circle", padding: 14 } } } },
  });
});

fetch("/api/forecast").then((r) => r.json()).then((d) => {
  if (!d.history.length) return;
  const labels = [...d.history.map((h) => h.ts), ...d.forecast.map((f) => f.ts)];
  const n = d.history.length, pad = Array(n).fill(null);
  new Chart($("fcChart"), {
    type: "line",
    data: { labels, datasets: [
      { label: "Actual / 10 min", data: [...d.history.map((h) => h.requests), ...d.forecast.map(() => null)], borderColor: "rgba(232,242,236,.55)", borderWidth: 1.25, pointRadius: 0 },
      { label: "Forecast", data: [...pad, ...d.forecast.map((f) => f.forecast)], borderColor: "#6ee7a6", borderWidth: 2.25, pointRadius: 0 },
      { label: "90% interval", data: [...pad, ...d.forecast.map((f) => f.upper)], borderColor: "rgba(110,231,166,.35)", borderWidth: 1, backgroundColor: "rgba(52,210,123,.16)", pointRadius: 0, fill: "+1" },
      { label: "lower", data: [...pad, ...d.forecast.map((f) => f.lower)], borderColor: "transparent", pointRadius: 0, fill: false },
    ] },
    options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { usePointStyle: true, pointStyle: "line", filter: (i) => i.text !== "lower" } } },
      scales: { x: { grid: { display: false }, border: { display: false }, ticks: { maxTicksLimit: 7, maxRotation: 0, callback: function (v) { return this.getLabelForValue(v).slice(5, 13).replace("T", " "); } } },
                y: { grid: { color: GRID }, border: { display: false } } } },
  });
});

fetch("/api/warmup").catch(() => {});

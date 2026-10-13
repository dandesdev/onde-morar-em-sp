/* Onde Morar Em SP: pick a daily destination, score every 250 m of the city, keep your own rental ads.
 * Data: site/data (built by scripts/build_site.py). Routing runs in router.js (Web Worker).
 */
(() => {
  "use strict";

  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => {
    try { const x = new URL(String(u)); return x.protocol === "https:" || x.protocol === "http:" ? x.href : null; } catch { return null; }
  };
  const brl = (v) => Math.round(v).toLocaleString("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 });
  const brlShort = (v) => (v >= 1000 ? `R$ ${(v / 1000).toFixed(1).replace(".", ",")} mil` : `R$ ${Math.round(v)}`);
  const fmtDate = (iso) => (iso ? String(iso).split("-").reverse().join("/") : "");
  const fmtMin = (v) => (v == null || !isFinite(v) ? "—" : `${Math.round(v)} min`);
  const isPhone = () => window.matchMedia("(max-width: 768px)").matches;
  const canHover = window.matchMedia("(hover: hover)").matches;
  const CITY_BBOX = [-46.84, -24.02, -46.35, -23.34];
  const inCity = (lat, lon) => lon >= CITY_BBOX[0] && lon <= CITY_BBOX[2] && lat >= CITY_BBOX[1] && lat <= CITY_BBOX[3];
  const metres = (lat1, lon1, lat2, lon2) =>
    Math.hypot((lon2 - lon1) * 111320 * Math.cos(((lat1 + lat2) / 2) * Math.PI / 180), (lat2 - lat1) * 110574);

  async function fetchJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    let buf = new Uint8Array(await res.arrayBuffer());
    if (buf[0] === 0x1f && buf[1] === 0x8b) {  // gzip that the server did not decode for us
      const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
      buf = new Uint8Array(await new Response(stream).arrayBuffer());
    }
    return JSON.parse(new TextDecoder().decode(buf));
  }

  // ---------------------------------------------------------------- colours, criteria, layers
  const BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"];
  const RED = ["#fbd9d6", "#f5b1ab", "#ee8a82", "#e34948", "#c23634", "#972726", "#6b1b1a"];
  const DIVERGING = ["#184f95", "#3987e5", "#9ec5f4", "#f0efec", "#f5b1ab", "#e34948", "#972726"];
  const FILTERED = "#d9d8d2";
  // data-quality alerts: categorical slots 1-3 + a neutral for "no alert"
  const ALERTS = [
    { col: "#f0efec", label: "Sem alerta" },
    { col: "#eb6834", label: "Crime provavelmente subnotificado" },
    { col: "#2a78d6", label: "Poucos registros de crime" },
    { col: "#1baf7a", label: "Comércio talvez incompleto no mapa" },
  ];
  const rgb = {};
  for (const h of [...BLUE, ...RED, ...DIVERGING, FILTERED, ...ALERTS.map((a) => a.col)]) rgb[h] = [1, 3, 5].map((k) => parseInt(h.slice(k, k + 2), 16));

  // Data-quality flags per square (bitmask), from data already in cells.json:
  //  1 few records: < 8 street robberies + thefts recorded within 400 m in 12 months
  //  2 likely under-reporting: < 1 theft per robbery (central SP records ~4 thefts per robbery;
  //    where thefts go unreported, the safety score looks better than reality)
  //  4 shops probably missing from OpenStreetMap: lit, built-up area with no market, bakery or pharmacy mapped in 600 m
  const DISC_KM2 = Math.PI * 0.4 * 0.4;
  function flagsOf(c) {
    let f = 0;
    if ((D.rob[c] + D.fur[c]) * DISC_KM2 < 8) f |= 1;
    else if (D.rob[c] > 0 && D.fur[c] / D.rob[c] < 1) f |= 2;
    if (D.nm[c] + D.np[c] + D.nf[c] === 0 && D.luz[c] >= 10) f |= 4;
    return f;
  }
  let FLAGS = null;

  // w = % of the score; linked sliders keep the sum at 100. Commute is a filter, not a weight.
  const CRITERIA = [
    { key: "ss", label: "Segurança na rua", w: 43, help: "Roubos e furtos a pedestres nos últimos 12 meses, comparado com o resto da cidade (100 = dos mais seguros). Onde aparece ⚠, os registros podem estar abaixo da realidade." },
    { key: "sg", label: "Árvores e verde", w: 21, help: "Verde visto por satélite + árvores de rua, comparado com o resto da cidade" },
    { key: "se", label: "Tudo a pé", w: 21, help: "Mercado, padaria, farmácia, praça e pet a até 5 min = 100; a 15 min ou mais = 0" },
    { key: "sc", label: "Rua calma", w: 15, help: "Longe de avenidas e com ruas locais em volta" },
  ];

  const LAYERS = [
    // colour classes come from the squares that pass the filters: "which of my options is best"
    { id: "score", label: "Nota geral, 0 a 100 (seus pesos)", get: (c) => S.score[c], dir: "up", fmt: (v) => v.toFixed(0) },
    { id: "commute", label: "Tempo porta a porta até o destino (min)", get: (c) => (S.times ? S.times[c] : null), dir: "low", fixed: [15, 20, 25, 30, 35, 45], fmt: (v) => v.toFixed(0), needsDest: true },
    { id: "ss", label: "Segurança (percentil, 100 = mais seguro)", get: (c) => D.ss[c], dir: "up", fmt: (v) => v.toFixed(0) },
    { id: "rob", label: "Roubos na rua / km² / ano (últimos 12 meses)", get: (c) => D.rob[c], dir: "bad", fmt: (v) => v.toFixed(0) },
    { id: "robn", label: "Roubos na rua à noite / km² / ano", get: (c) => D.robn[c], dir: "bad", fmt: (v) => v.toFixed(0) },
    { id: "trend", label: "Tendência dos roubos (últimos 12 meses vs anos-base, %)", get: (c) => (D.rob0[c] >= 15 ? (100 * (D.rob[c] - D.rob0[c])) / D.rob0[c] : null), dir: "div", fixed: [-40, -20, -5, 5, 20, 40], fmt: (v) => (v > 0 ? "+" : "") + v.toFixed(0) },
    { id: "sg", label: "Verde (satélite + árvores de rua)", get: (c) => D.sg[c], dir: "up", fmt: (v) => v.toFixed(0) },
    { id: "se", label: "Tudo a pé (0-100)", get: (c) => D.se[c], dir: "up", fmt: (v) => v.toFixed(0) },
    { id: "sc", label: "Rua calma (0-100)", get: (c) => D.sc[c], dir: "up", fmt: (v) => v.toFixed(0) },
    { id: "luz", label: "Postes de iluminação num raio de 150 m", get: (c) => D.luz[c], dir: "up", fmt: (v) => v.toFixed(0) },
    { id: "alerts", label: "⚠ Alertas de dados (subnotificação, dados escassos)", cat: true,
      get: (c) => { const f = FLAGS[c]; return f & 2 ? 1 : f & 1 ? 2 : f & 4 ? 3 : 0; }, fmt: (v) => ALERTS[v].label },
  ];

  const S = {
    dest: null,              // {lat, lon, name}
    scenario: "now",
    weights: Object.fromEntries(CRITERIA.map((c) => [c.key, c.w])),
    maxc: 35, nearMetro: false, metroD: 800, layer: "score",
    times: null, timesKey: null,
    score: null, ok: null,
    picking: null,           // callback while the user picks a point on the map
  };
  let D = null;              // cells.json
  let META = {};             // meta.json (dates shown in the source lines)
  let N = 0;

  // ---------------------------------------------------------------- map
  const map = L.map("map", { preferCanvas: false, zoomControl: true, maxZoom: 19 }).setView([-23.56, -46.64], 11);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Base: Esri, HERE, Garmin, &copy; OpenStreetMap", maxZoom: 16,
  }).addTo(map);
  // street-level detail from z17 (tile.openstreetmap.org's policy blocks apps like this one)
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Ruas: Esri", minZoom: 17, maxZoom: 19, opacity: 0.85,
  }).addTo(map);
  map.createPane("cells").style.zIndex = 390;
  map.createPane("over").style.zIndex = 420;
  map.createPane("pins").style.zIndex = 640;
  map.createPane("labels").style.zIndex = 650;
  const svgOver = L.svg({ pane: "over" });

  // ---------------------------------------------------------------- boot
  const worker = new Worker("router.js?v=1");
  const pending = new Map();
  let reqId = 0;
  worker.onmessage = (e) => { const m = e.data; const p = pending.get(m.id ?? "init"); if (p) { pending.delete(m.id ?? "init"); m.type === "error" ? p.reject(new Error(m.message)) : p.resolve(m); } };
  const ask = (msg) => new Promise((resolve, reject) => { const id = msg.type === "init" ? "init" : ++reqId; pending.set(id, { resolve, reject }); worker.postMessage({ ...msg, id }); });

  let lookup, cellLat, cellLon, stDistNow, stDistL6, stNameNow, stNameL6, overlay, canvas, ctx, img, bairroMeta = {};
  let workerReady;

  boot().catch((err) => {
    console.error(err);
    $("loading").textContent = "Não foi possível carregar os dados. Recarregue a página.";
  });

  async function boot() {
    const [cells, bairros, rail, stations, meta] = await Promise.all([
      fetchJSON("data/cells.json.gz"), fetchJSON("data/bairros.json.gz"), fetchJSON("data/rail.json.gz"),
      fetchJSON("data/stations.json"), fetchJSON("data/meta.json"),
    ]);
    D = cells; N = cells.n;
    META = meta;
    showDataDates(meta);
    FLAGS = new Uint8Array(N);
    for (let c = 0; c < N; c++) FLAGS[c] = flagsOf(c);
    workerReady = ask({ type: "init", cells: { i: cells.i, j: cells.j, grid: cells.grid } });
    const g = cells.grid;
    lookup = new Int32Array(g.ncols * g.nrows).fill(-1);
    cellLat = new Float64Array(N); cellLon = new Float64Array(N);
    for (let c = 0; c < N; c++) {
      lookup[cells.i[c] + cells.j[c] * g.ncols] = c;
      cellLon[c] = g.lon0 + (cells.i[c] + 0.5) * g.dlon;
      cellLat[c] = g.lat0 + (cells.j[c] + 0.5) * g.dlat;
    }
    nearestStations(stations);
    setupCells();
    setupOverlays(bairros, rail, stations);
    bairros.features.forEach((f) => { bairroMeta[f.properties.bairro] = f.properties; });
    setupPanel(meta);
    Imoveis.init();
    restoreFromUrl();
    update();
    $("loading").hidden = true;
    await workerReady;
    if (S.dest) computeTimes();
  }

  function nearestStations(stations) {
    stDistNow = new Float32Array(N); stDistL6 = new Float32Array(N);
    stNameNow = new Array(N); stNameL6 = new Array(N);
    const sl = stations.map((s) => ({ ...s, kx: 111320 * Math.cos(s.lat * Math.PI / 180) }));
    for (let c = 0; c < N; c++) {
      let bn = Infinity, b6 = Infinity, nn = "", n6 = "";
      for (const s of sl) {
        const d = Math.hypot((s.lon - cellLon[c]) * s.kx, (s.lat - cellLat[c]) * 110574);
        if (s.status === "op" && d < bn) { bn = d; nn = s.name; }
        if (d < b6) { b6 = d; n6 = s.name; }
      }
      stDistNow[c] = bn; stDistL6[c] = b6; stNameNow[c] = nn; stNameL6[c] = n6;
    }
  }

  // ---------------------------------------------------------------- cells as one image
  function setupCells() {
    const g = D.grid;
    canvas = document.createElement("canvas");
    canvas.width = g.ncols; canvas.height = g.nrows;
    ctx = canvas.getContext("2d");
    img = ctx.createImageData(g.ncols, g.nrows);
    const bounds = [[g.lat0, g.lon0], [g.lat0 + g.nrows * g.dlat, g.lon0 + g.ncols * g.dlon]];
    overlay = L.imageOverlay(canvas.toDataURL(), bounds, { pane: "cells", className: "cells-img", interactive: false }).addTo(map);

    map.on("click", (e) => {
      if (S.picking) { const cb = S.picking; stopPicking(); cb(e.latlng); return; }
      const c = cellAt(e.latlng);
      if (c >= 0) showCell(c, e.latlng);
    });
    if (canHover) {
      const tip = L.tooltip({ direction: "top", offset: [0, -6], opacity: 0.95 });
      let raf = 0, lastLL = null;
      map.on("mousemove", (e) => {
        lastLL = e.latlng;
        if (raf) return;
        raf = requestAnimationFrame(() => {
          raf = 0;
          const c = S.picking ? -1 : cellAt(lastLL);
          if (c < 0) { map.closeTooltip(tip); return; }
          const layer = currentLayer(), v = layer.get(c);
          tip.setLatLng(lastLL).setContent(`<b>${esc(bairroName(c))}</b> <span class="d">(${esc(distritoName(c))})</span><br>${esc(layer.label)}: <b>${v == null || !isFinite(v) ? "—" : layer.fmt(v)}</b>${S.ok[c] ? "" : '<br><span class="d">fora dos filtros</span>'}`);
          if (!map.hasLayer(tip)) tip.addTo(map);
        });
      });
      map.on("mouseout", () => map.closeTooltip(tip));
    }
  }
  function cellAt(ll) {
    const g = D.grid;
    const i = Math.floor((ll.lng - g.lon0) / g.dlon), j = Math.floor((ll.lat - g.lat0) / g.dlat);
    if (i < 0 || j < 0 || i >= g.ncols || j >= g.nrows) return -1;
    return lookup[i + j * g.ncols];
  }
  const bairroName = (c) => (D.b[c] >= 0 ? D.bairros[D.b[c]] : "—");
  const distritoName = (c) => (D.d[c] >= 0 ? D.distritos[D.d[c]] : "—");
  const currentLayer = () => LAYERS.find((l) => l.id === S.layer);

  // ---------------------------------------------------------------- scoring + filters
  function recompute() {
    const w = S.weights, tot = Object.values(w).reduce((a, b) => a + b, 0) || 1;
    if (!S.score) { S.score = new Float32Array(N); S.ok = new Uint8Array(N); }
    const st = S.scenario === "l6" ? stDistL6 : stDistNow;
    for (let c = 0; c < N; c++) {
      S.score[c] = (w.ss * D.ss[c] + w.sg * D.sg[c] + w.se * D.se[c] + w.sc * D.sc[c]) / tot;
      const timeOk = !S.dest || !S.times || S.times[c] <= S.maxc;
      S.ok[c] = timeOk && (!S.nearMetro || st[c] <= S.metroD) ? 1 : 0;
    }
  }

  let breaks = [], palette = BLUE;
  function classify() {
    const layer = currentLayer();
    if (layer.cat) { breaks = []; return; }
    palette = layer.dir === "bad" ? RED : layer.dir === "div" ? DIVERGING : BLUE;
    if (layer.fixed) { breaks = layer.fixed; return; }
    let vals = [];
    for (let c = 0; c < N; c++) if (S.ok[c]) { const v = layer.get(c); if (v != null && isFinite(v)) vals.push(v); }
    if (vals.length < 20) { vals = []; for (let c = 0; c < N; c++) { const v = layer.get(c); if (v != null && isFinite(v)) vals.push(v); } }
    vals.sort((a, b) => a - b);
    breaks = [1, 2, 3, 4, 5, 6].map((k) => vals[Math.floor((k * vals.length) / 7)] ?? 0);
  }
  function colorFor(v, layer) {
    if (v == null || !isFinite(v)) return null;
    if (layer.cat) return ALERTS[v].col;
    let k = 0;
    while (k < breaks.length && v >= breaks[k]) k++;
    if (layer.dir === "low") k = palette.length - 1 - k;
    return palette[k];
  }
  function paint() {
    const layer = currentLayer(), g = D.grid, px = img.data;
    px.fill(0);
    for (let c = 0; c < N; c++) {
      const v = layer.get(c);
      const col = S.ok[c] ? colorFor(v, layer) : null;
      const [r, gg, b] = rgb[col || FILTERED];
      const o = ((g.nrows - 1 - D.j[c]) * g.ncols + D.i[c]) * 4;
      px[o] = r; px[o + 1] = gg; px[o + 2] = b; px[o + 3] = col ? 178 : 90;
    }
    ctx.putImageData(img, 0, 0);
    overlay.setUrl(canvas.toDataURL());
  }

  // ---------------------------------------------------------------- destination + routing
  function setDestination(dest, opts = {}) {
    if (!inCity(dest.lat, dest.lon)) {
      alert("Por enquanto o site cobre só a cidade de São Paulo. Escolha um ponto dentro do município.");
      return;
    }
    S.dest = { lat: dest.lat, lon: dest.lon, name: (dest.name || "Destino").slice(0, 40) };
    S.times = null; S.timesKey = null;
    drawDestMarker();
    $("dest-current").hidden = false;
    $("dest-name").value = S.dest.name;
    $("dest-share").disabled = false;
    $("maxc").disabled = false;
    $("maxc-help").textContent = "Quadrados mais longe que isso ficam cinza e saem do ranking.";
    if (!opts.keepView) map.setView([S.dest.lat, S.dest.lon], Math.max(map.getZoom(), 13));
    writeUrl();
    updateTagline();
    computeTimes();
  }
  let destMarker = null;
  function drawDestMarker() {
    const icon = L.divIcon({ className: "", html: `<div class="dest-icon">${esc(S.dest.name)}</div>`, iconSize: [0, 0] });
    if (!destMarker) {
      destMarker = L.marker([S.dest.lat, S.dest.lon], { icon, draggable: true, pane: "labels", keyboard: false, autoPan: true })
        .addTo(map)
        .on("dragend", () => { const ll = destMarker.getLatLng(); setDestination({ lat: ll.lat, lon: ll.lng, name: S.dest.name }, { keepView: true }); });
      destMarker.bindTooltip("Arraste para ajustar o destino", { direction: "bottom", offset: [0, 10] });
    } else {
      destMarker.setLatLng([S.dest.lat, S.dest.lon]).setIcon(icon);
    }
  }
  async function computeTimes() {
    if (!S.dest || !workerReady) return;
    const key = `${S.dest.lat.toFixed(5)},${S.dest.lon.toFixed(5)},${S.scenario}`;
    if (S.timesKey === key) return;
    S.timesKey = key;
    $("dest-status").textContent = "Calculando tempos de transporte…";
    await workerReady;
    const r = await ask({ type: "route", dest: { lat: S.dest.lat, lon: S.dest.lon }, scenario: S.scenario });
    if (S.timesKey !== key) return;  // a newer request superseded this one
    S.times = r.times;
    let reach = 0;
    for (let c = 0; c < N; c++) if (S.times[c] <= 45) reach++;
    $("dest-status").textContent = `${reach.toLocaleString("pt-BR")} quadras a até 45 min (calculado em ${r.ms} ms).`;
    update();
    Imoveis.render();
  }
  function updateTagline() {
    $("tagline").textContent = S.dest
      ? `Onde morar perto de ${S.dest.name}: cada quadrado de 250 m avaliado pelos seus critérios.`
      : "Escolha para onde você vai todo dia. O mapa mostra, quadra a quadra, onde vale a pena morar.";
    document.title = S.dest ? `Onde morar perto de ${S.dest.name} · Onde Morar Em SP` : "Onde Morar Em SP";
  }
  function writeUrl() {
    const p = new URLSearchParams();
    if (S.dest) { p.set("d", `${S.dest.lat.toFixed(5)},${S.dest.lon.toFixed(5)}`); p.set("n", S.dest.name); }
    if (S.scenario !== "now") p.set("s", S.scenario);
    const q = p.toString();
    history.replaceState(null, "", location.pathname + (q ? `?${q}` : "") + location.hash);
  }
  function restoreFromUrl() {
    const p = new URLSearchParams(location.search);
    if (p.get("s") === "l6") setScenario("l6", true);
    const d = (p.get("d") || "").split(",").map(Number);
    if (d.length === 2 && d.every(isFinite)) setDestination({ lat: d[0], lon: d[1], name: p.get("n") || "Destino" });
  }

  // geocoding: Photon (komoot), OpenStreetMap data
  async function geocode(q) {
    const url = `https://photon.komoot.io/api/?q=${encodeURIComponent(q)}&limit=7&bbox=${CITY_BBOX.join(",")}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error("busca indisponível");
    const js = await res.json();
    return js.features.map((f) => {
      const p = f.properties, [lon, lat] = f.geometry.coordinates;
      const main = p.name || [p.street, p.housenumber].filter(Boolean).join(", ") || "Ponto";
      const sub = [p.name && p.street ? [p.street, p.housenumber].filter(Boolean).join(", ") : null, p.district || p.locality, p.city].filter(Boolean).join(" · ");
      return { lat, lon, main, sub };
    }).filter((r) => inCity(r.lat, r.lon));
  }
  async function reverseGeocode(lat, lon) {
    try {
      const res = await fetch(`https://photon.komoot.io/reverse?lat=${lat}&lon=${lon}&limit=1`);
      const f = (await res.json()).features[0];
      if (!f) return null;
      const p = f.properties;
      return p.name || [p.street, p.housenumber].filter(Boolean).join(", ") || p.district || null;
    } catch { return null; }
  }
  function searchBox(input, list, onPick) {
    let timer = 0, seq = 0, items = [];
    const close = () => { list.hidden = true; list.innerHTML = ""; };
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 3) { close(); return; }
      timer = setTimeout(async () => {
        const my = ++seq;
        try {
          items = await geocode(q);
          if (my !== seq) return;
          list.innerHTML = items.length
            ? items.map((r, k) => `<li data-k="${k}">${esc(r.main)}<span class="d">${esc(r.sub)}</span></li>`).join("")
            : `<li class="d">Nada encontrado em São Paulo.</li>`;
          list.hidden = false;
        } catch { list.innerHTML = `<li class="d">Busca indisponível agora. Use "Escolher no mapa".</li>`; list.hidden = false; }
      }, 350);
    });
    list.addEventListener("click", (e) => {
      const li = e.target.closest("li[data-k]");
      if (!li) return;
      const r = items[+li.dataset.k];
      close();
      onPick(r);
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    document.addEventListener("click", (e) => { if (!list.contains(e.target) && e.target !== input) close(); });
  }

  function startPicking(cb, text) {
    S.picking = cb;
    $("pick-banner").firstChild.textContent = (text || "Toque no mapa para escolher o ponto") + " ";
    $("pick-banner").hidden = false;
    document.body.classList.add("picking");
    if (isPhone()) window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function stopPicking() {
    S.picking = null;
    $("pick-banner").hidden = true;
    document.body.classList.remove("picking");
  }

  // ---------------------------------------------------------------- details (popup / bottom sheet)
  let selRect = null;
  function select(c) {
    if (selRect) { map.removeLayer(selRect); selRect = null; }
    if (c == null || c < 0) return;
    const g = D.grid, lat = cellLat[c], lon = cellLon[c];
    selRect = L.rectangle([[lat - g.dlat / 2, lon - g.dlon / 2], [lat + g.dlat / 2, lon + g.dlon / 2]],
      { pane: "over", renderer: svgOver, weight: 3, color: "#0b0b0b", fill: false, interactive: false }).addTo(map);
  }
  // Details always open in one panel centred on the page (scrolls inside if long), on every screen size.
  function openDetails(html) {
    $("sheet-body").innerHTML = html;
    $("sheet").hidden = false;
    $("sheet").scrollTop = 0;
    $("sheet-close").focus({ preventScroll: true });
  }
  const closeDetails = () => { $("sheet").hidden = true; select(null); };
  $("sheet-close").addEventListener("click", closeDetails);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("sheet").hidden) closeDetails(); });

  function cellSummary(c) {
    const trend = D.rob0[c] >= 15 ? `${D.rob[c] >= D.rob0[c] ? "+" : ""}${Math.round((100 * (D.rob[c] - D.rob0[c])) / D.rob0[c])}% vs ${esc(META.base_label)}` : "poucos casos para tendência";
    const st = S.scenario === "l6" ? [stNameL6[c], stDistL6[c]] : [stNameNow[c], stDistNow[c]];
    return `
      <div class="d">Distrito ${esc(distritoName(c))} · estação mais próxima: ${esc(st[0])} (${Math.round(st[1])} m em linha reta)</div>
      <div class="scores">
        <div>Nota<b>${Math.round(S.score[c])}</b></div><div>Segur.<b>${D.ss[c]}${FLAGS[c] & 3 ? " ⚠" : ""}</b></div><div>Verde<b>${D.sg[c]}</b></div><div>A pé<b>${D.se[c]}</b></div><div>Calma<b>${D.sc[c]}</b></div>
      </div>
      ${warningsHtml(c)}
      <div class="route" data-route="${c}">${S.dest ? (S.times ? `<b>${fmtMin(S.times[c])}</b> até ${esc(S.dest.name)} · calculando o trajeto…` : "Calculando…") : "Escolha um destino para ver o tempo de transporte."}</div>
      <table>
        <tr><td>Supermercado / hortifruti</td><td>${fmtMin(D.tm[c])} a pé · ${D.nm[c]} em 600 m</td></tr>
        <tr><td>Padaria</td><td>${fmtMin(D.tp[c])} · ${D.np[c]} em 600 m</td></tr>
        <tr><td>Farmácia</td><td>${fmtMin(D.tf[c])} · ${D.nf[c]} em 600 m</td></tr>
        <tr><td>Praça ou parque (cachorro)</td><td>${fmtMin(D.tv[c])} · parque grande ${fmtMin(D.tpk[c])}</td></tr>
        <tr><td>Pet shop / veterinário</td><td>${fmtMin(D.tpet[c])}</td></tr>
        <tr><td>Feira livre</td><td>${fmtMin(D.tfe[c])}</td></tr>
        <tr><td>Roubos na rua (12 meses)</td><td>${D.rob[c]}/km² · à noite ${D.robn[c]}/km² · ${trend}</td></tr>
        <tr><td>Furtos na rua (12 meses)</td><td>${D.fur[c]}/km²</td></tr>
        <tr><td>Crimes violentos 2024-26</td><td>${D.vio[c]}/km²/ano</td></tr>
        <tr><td>Roubo de veículo (12 meses)</td><td>${D.rveh[c]}/km²</td></tr>
        <tr><td>Verde</td><td>NDVI ${D.ndvi[c]} · copa ${D.can[c]}% · ${D.arv[c]} árvores de rua em 200 m</td></tr>
        <tr><td>Avenida/arterial mais próxima</td><td>${D.dbig[c]} m · ${D.loc[c]}% das ruas em 200 m são locais</td></tr>
        <tr><td>Postes de luz em 150 m</td><td>${D.luz[c]}</td></tr>
      </table>
      <div class="src">Fontes: crime SSP-SP (${esc(META.crime_window)}) · verde Sentinel-2 (${esc(fmtDate(META.ndvi_date))}) e Prefeitura · comércio OpenStreetMap · ruas CET · transporte SPTrans, Metrô e CPTM via GeoSampa. <a href="fontes.html" target="_blank" rel="noopener">Ver fontes e método</a></div>`;
  }
  function warningsHtml(c) {
    const f = FLAGS[c];
    if (!f) return "";
    const items = [];
    if (f & 2) items.push(`<b>Crime provavelmente subnotificado.</b> Aqui há ${(D.fur[c] / D.rob[c]).toFixed(1).replace(".", ",")} furto registrado por roubo; no centro são cerca de 4. Onde quase ninguém registra furto, a segurança costuma parecer melhor do que é.`);
    if (f & 1) items.push(`<b>Poucos registros de crime</b> por perto (menos de 8 em 12 meses num raio de 400 m). Pode ser um lugar tranquilo e pouco movimentado, ou um lugar onde as ocorrências não são registradas.`);
    if (f & 4) items.push(`<b>Comércio talvez incompleto no mapa.</b> A região é urbanizada, mas nenhum mercado, padaria ou farmácia aparece no OpenStreetMap a até 600 m. A nota "Tudo a pé" pode estar baixa demais.`);
    return `<div class="warn" role="note"><span aria-hidden="true">⚠</span><div>${items.join("<br>")} <a href="fontes.html#alertas" target="_blank" rel="noopener">Saiba mais</a></div></div>`;
  }
  async function fillRoute(c) {
    if (!S.dest || !S.times) return;
    const key = S.timesKey;
    const r = await ask({ type: "path", cell: c });
    if (key !== S.timesKey || !r.path) return;
    const p = r.path;
    const legs = p.legs.map((l) => (l.t === "r"
      ? `<span class="leg" style="background:${esc(l.col || "#52514e")}">${esc(l.c)}</span> ${esc(l.from)} → ${esc(l.to)}`
      : `<span class="leg bus">${esc(l.c)}</span> ${esc(l.n)}`)).join("<br>");
    const html = `<b>${fmtMin(p.total)}</b> até ${esc(S.dest.name)}${legs ? "" : " a pé"}<br>${legs}${legs ? "<br>" : ""}<span class="d">caminhada ~${Math.round(p.walk)} min · espera e estação ~${Math.round(p.wait)} min</span>`;
    document.querySelectorAll(`[data-route="${c}"]`).forEach((el) => { el.innerHTML = html; });
  }
  function showCell(c, latlng) {
    select(c);
    openDetails(`<div class="pop"><h3>${esc(bairroName(c))}</h3>${cellSummary(c)}</div>`, latlng);
    fillRoute(c);
  }

  // ---------------------------------------------------------------- overlays
  let labelData = [];
  const labelLayer = L.layerGroup();
  let bairroLayer, distritoLayer, railLayer, stationMarkers = [];
  const lazy = {};

  function setupOverlays(bairros, rail, stations) {
    bairroLayer = L.geoJSON(bairros, { pane: "over", renderer: svgOver, interactive: false,
      style: { color: "#3d3d3a", weight: 1, opacity: 0.5, fill: false, dashArray: "3 3" } });
    // label each bairro on its member cell closest to the zone's mean centre
    const acc = new Map();
    for (let c = 0; c < N; c++) {
      const b = D.b[c]; if (b < 0) continue;
      const a = acc.get(b) || { n: 0, lat: 0, lon: 0 };
      a.n++; a.lat += cellLat[c]; a.lon += cellLon[c]; acc.set(b, a);
    }
    const best = new Map();
    for (let c = 0; c < N; c++) {
      const b = D.b[c]; if (b < 0) continue;
      const a = acc.get(b), d = (cellLat[c] - a.lat / a.n) ** 2 + (cellLon[c] - a.lon / a.n) ** 2;
      if (!best.has(b) || d < best.get(b)[0]) best.set(b, [d, c]);
    }
    labelData = [...best.entries()].map(([b, [, c]]) => ({ name: D.bairros[b], lat: cellLat[c], lon: cellLon[c], n: acc.get(b).n }));
    map.on("moveend zoomend", syncLabels);

    railLayer = L.layerGroup();
    L.geoJSON(rail, { pane: "over", renderer: svgOver,
      style: (f) => ({ color: f.properties.color, weight: 4, opacity: 0.95, dashArray: f.properties.status === "l6" ? "8 6" : null }),
      onEachFeature: (f, l) => l.bindTooltip(esc(f.properties.line) + (f.properties.status === "l6" ? " (obras / operação assistida)" : ""), { sticky: true }),
    }).addTo(railLayer);
    stations.forEach((s) => {
      const m = L.circleMarker([s.lat, s.lon], { pane: "over", renderer: svgOver, radius: 5, color: "#0b0b0b", weight: 2, fillColor: "#fff", fillOpacity: 1, dashArray: s.status === "l6" ? "2 2" : null })
        .bindTooltip(`<b>${esc(s.name)}</b><br>${s.lines.map(esc).join("<br>")}`, { direction: "top" });
      m._label = L.tooltip({ permanent: true, direction: "right", offset: [6, 0], className: "st-label", pane: "labels" }).setLatLng([s.lat, s.lon]).setContent(esc(s.name));
      stationMarkers.push(m);
      m.addTo(railLayer);
    });
    map.on("zoomend", syncStationLabels);
  }
  function syncLabels() {
    labelLayer.clearLayers();
    const on = $("ov-bairros").checked, z = map.getZoom();
    if (on && !map.hasLayer(labelLayer)) labelLayer.addTo(map);
    if (!on || z < 13) return;
    const b = map.getBounds().pad(0.1);
    let shown = 0;
    for (const l of labelData) {
      if ((z < 14 && l.n < 8) || !b.contains([l.lat, l.lon])) continue;
      L.tooltip({ permanent: true, direction: "center", className: "bairro-label", pane: "labels", interactive: false })
        .setLatLng([l.lat, l.lon]).setContent(esc(l.name)).addTo(labelLayer);
      if (++shown > 160) break;
    }
  }
  function syncStationLabels() {
    const on = map.hasLayer(railLayer) && map.getZoom() >= 14;
    stationMarkers.forEach((m) => { if (on && !map.hasLayer(m._label)) m._label.addTo(map); if (!on && map.hasLayer(m._label)) map.removeLayer(m._label); });
  }
  async function lazyLayer(name) {
    if (lazy[name]) return lazy[name];
    const data = await fetchJSON(`data/${name}.json.gz`);
    if (name === "greens") lazy[name] = L.geoJSON(data, { pane: "over", renderer: svgOver, interactive: false, style: { color: "#008300", weight: 1, fillColor: "#008300", fillOpacity: 0.25 } });
    if (name === "bigroads") lazy[name] = L.geoJSON(data, { pane: "over", renderer: svgOver, interactive: false, style: { color: "#52514e", weight: 2.5, opacity: 0.7 } });
    if (name === "distritos") lazy[name] = L.geoJSON(data, { pane: "over", renderer: svgOver, interactive: false, style: { color: "#0b0b0b", weight: 2, opacity: 0.5, fill: false } });
    if (name === "pois") {
      const STY = { supermarket: ["Supermercado", "#2a78d6"], hortifruti: ["Hortifruti", "#2a78d6"], minimarket: ["Mercadinho", "#2a78d6"], butcher: ["Açougue", "#2a78d6"],
        market: ["Mercado", "#2a78d6"], feira: ["Feira livre", "#2a78d6"], bakery: ["Padaria", "#eb6834"], pharmacy: ["Farmácia", "#1baf7a"],
        vet: ["Veterinário", "#6250d6"], petshop: ["Pet shop", "#6250d6"], dog_park: ["Cachorródromo", "#6250d6"] };
      const all = data.filter((p) => STY[p.c]);
      const grp = L.layerGroup();
      grp._all = all; grp._sty = STY;
      lazy[name] = grp;
    }
    return lazy[name];
  }
  function syncPois() {
    const grp = lazy.pois;
    if (!grp) return;
    grp.clearLayers();
    if (!$("ov-pois").checked || map.getZoom() < 15) return;
    const b = map.getBounds().pad(0.2);
    for (const p of grp._all) {
      if (!b.contains([p.y, p.x])) continue;
      const s = grp._sty[p.c];
      L.circleMarker([p.y, p.x], { pane: "over", renderer: svgOver, radius: 5, color: "#fff", weight: 2, fillColor: s[1], fillOpacity: 1 })
        .bindTooltip(esc(s[0] + (p.n ? ": " + p.n : ""))).addTo(grp);
    }
  }

  // ---------------------------------------------------------------- side panel
  function setScenario(v, silent) {
    S.scenario = v;
    document.querySelectorAll("#scenario button").forEach((x) => x.classList.toggle("on", x.dataset.v === v));
    if (!silent) { writeUrl(); computeTimes(); update(); }
  }

  function setupPanel(meta) {
    searchBox($("dest-q"), $("dest-results"), (r) => { $("dest-q").value = ""; setDestination({ lat: r.lat, lon: r.lon, name: r.main }); });
    $("dest-pick").addEventListener("click", () => startPicking(async (ll) => {
      setDestination({ lat: ll.lat, lon: ll.lng, name: "Ponto escolhido" }, { keepView: true });
      const name = await reverseGeocode(ll.lat, ll.lng);
      if (name && S.dest && S.dest.name === "Ponto escolhido") { S.dest.name = name.slice(0, 40); $("dest-name").value = S.dest.name; drawDestMarker(); writeUrl(); updateTagline(); }
    }, "Toque no mapa onde fica o seu destino"));
    $("pick-cancel").addEventListener("click", stopPicking);
    $("dest-name").addEventListener("input", () => {
      if (!S.dest) return;
      S.dest.name = $("dest-name").value.trim().slice(0, 40) || "Destino";
      drawDestMarker(); writeUrl(); updateTagline();
    });
    $("dest-share").addEventListener("click", async () => {
      const url = location.origin + location.pathname + location.search;
      try { await navigator.clipboard.writeText(url); flash($("dest-share"), "Link copiado!"); }
      catch { prompt("Copie o link:", url); }
    });

    document.querySelectorAll("#scenario button").forEach((b) => b.addEventListener("click", () => setScenario(b.dataset.v)));

    const maxc = $("maxc");
    maxc.addEventListener("input", () => { S.maxc = +maxc.value; scheduleUpdate(); });
    $("nearmetro").addEventListener("change", (e) => { S.nearMetro = e.target.checked; scheduleUpdate(); });
    $("metrod").addEventListener("input", (e) => { S.metroD = +e.target.value; scheduleUpdate(); });

    // weights: four linked sliders that always sum to 100
    const wbox = $("weights");
    wbox.insertAdjacentHTML("beforeend", `<p class="hint">Cada quadrado ganha uma nota de 0 a 100. Os quatro pesos sempre somam 100%: ao subir um, os outros descem na mesma proporção. Zero = ignorar o critério.</p>`);
    CRITERIA.forEach((c) => wbox.insertAdjacentHTML("beforeend",
      `<label class="row" for="w-${c.key}"><span>${c.label}</span><output id="w-${c.key}-o"></output></label><div class="d">${c.help}</div>
       <input type="range" min="0" max="100" step="1" value="${c.w}" id="w-${c.key}">`));
    CRITERIA.forEach((c) => $(`w-${c.key}`).addEventListener("input", (e) => { setWeight(c.key, +e.target.value); syncWeights(); scheduleUpdate(); }));
    syncWeights();

    const sel = $("layer");
    LAYERS.forEach((l) => sel.insertAdjacentHTML("beforeend", `<option value="${l.id}">${esc(l.label)}</option>`));
    sel.addEventListener("change", () => { S.layer = sel.value; update(); });

    const toggle = (id, getLayer, after) => {
      const el = $(id);
      const apply = async () => {
        const layer = getLayer && (await getLayer());
        if (layer) { if (el.checked) layer.addTo(map); else map.removeLayer(layer); }
        if (after) after();
      };
      el.addEventListener("change", apply);
      if (el.checked) apply();
    };
    toggle("ov-bairros", () => bairroLayer, syncLabels);
    toggle("ov-rail", () => railLayer, syncStationLabels);
    toggle("ov-imoveis", () => Imoveis.layer);
    toggle("ov-green", () => lazyLayer("greens"));
    toggle("ov-roads", () => lazyLayer("bigroads"));
    toggle("ov-distritos", () => lazyLayer("distritos"));
    toggle("ov-pois", () => lazyLayer("pois"), syncPois);
    map.on("moveend", syncPois);

    map.attributionControl.setPrefix('<a href="fontes.html" target="_blank" rel="noopener">Fontes dos dados</a> · <a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
  }

  function showDataDates(m) {
    const d = m.built ? new Date(m.built).toLocaleDateString("pt-BR") : "—";
    const [y, mo] = String(m.crime_last || "").split("-");
    const months = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];
    $("updated").textContent = `Dados atualizados em ${d}${mo ? ` · crimes até ${months[+mo - 1]}/${y}` : ""} · atualização automática toda semana`;
    const trend = LAYERS.find((l) => l.id === "trend");
    if (m.base_label) trend.label = `Tendência dos roubos (últimos 12 meses vs ${m.base_label}, %)`;
    const th = $("rank").querySelector("th:last-child");
    if (th && m.base_label) th.title = `Últimos 12 meses vs média de ${m.base_label}`;
  }

  function setWeight(key, value) {
    const others = CRITERIA.map((c) => c.key).filter((k) => k !== key);
    const rest = 100 - value;
    const sum = others.reduce((a, k) => a + S.weights[k], 0);
    const raw = others.map((k) => (sum > 0 ? (S.weights[k] * rest) / sum : rest / others.length));
    const fl = raw.map(Math.floor);
    let left = rest - fl.reduce((a, b) => a + b, 0);
    raw.map((v, i) => [v - fl[i], i]).sort((a, b) => b[0] - a[0]).forEach(([, i]) => { if (left > 0) { fl[i]++; left--; } });
    S.weights[key] = value;
    others.forEach((k, i) => { S.weights[k] = fl[i]; });
  }
  function syncWeights() {
    CRITERIA.forEach((c) => { $(`w-${c.key}`).value = S.weights[c.key]; $(`w-${c.key}-o`).textContent = `${S.weights[c.key]}% da nota`; });
  }
  function flash(btn, text) {
    const old = btn.textContent; btn.textContent = text;
    setTimeout(() => { btn.textContent = old; }, 1800);
  }

  function legend() {
    const layer = currentLayer();
    if (layer.cat) {
      $("legend").innerHTML = ALERTS.map((a) => `<div><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${a.col};vertical-align:-1px;margin-right:6px"></span>${esc(a.label)}</div>`).join("")
        + `<div class="d" style="margin-top:4px">Quando há mais de um alerta, vale o primeiro da lista. <a href="fontes.html#alertas" target="_blank" rel="noopener">Como os alertas são calculados</a></div>`;
      return;
    }
    const cols = layer.dir === "low" ? [...palette].reverse() : palette;
    const words = { low: ["mais rápido", "mais demorado"], bad: ["menos", "mais"], div: ["caindo", "subindo"], up: ["pior", "melhor"] }[layer.dir];
    $("legend").innerHTML = `<div class="bar">${cols.map((c) => `<span style="background:${c}"></span>`).join("")}</div>
      <div class="ticks">${breaks.map((t, i) => `<span style="left:${(100 * (i + 1)) / 7}%">${layer.fmt(t)}</span>`).join("")}</div>
      <div class="dir"><span>${words[0]}</span><span>${words[1]}</span></div>
      <div class="d" style="margin-top:4px"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${FILTERED};vertical-align:-1px"></span> fora dos filtros</div>`;
  }

  function ranking() {
    const acc = new Map();
    for (let c = 0; c < N; c++) {
      const b = D.b[c]; if (b < 0) continue;
      let a = acc.get(b);
      if (!a) { a = { n: 0, ok: 0, s: 0, rob: 0, t: [], lat: 0, lon: 0, fc: 0 }; acc.set(b, a); }
      a.n++; a.lat += cellLat[c]; a.lon += cellLon[c];
      if (!S.ok[c]) continue;
      a.ok++; a.s += S.score[c]; a.rob += D.rob[c];
      if (FLAGS[c] & 3) a.fc++;
      if (S.times) a.t.push(S.times[c]);
    }
    const rows = [...acc.entries()].filter(([, a]) => a.ok >= 3 && a.ok / a.n >= 0.4).map(([b, a]) => {
      a.t.sort((x, y) => x - y);
      const name = D.bairros[b], bm = bairroMeta[name] || {};
      const by = META.base_years || [], base = by.reduce((s, y) => s + (+bm[String(y)] || 0), 0) / (by.length || 1), cur = +bm["12m"] || 0;
      return { name, d: bm.distrito, score: a.s / a.ok, t: a.t.length ? a.t[a.t.length >> 1] : null, rob: a.rob / a.ok,
               tr: base >= 20 ? (100 * (cur - base)) / base : null, lat: a.lat / a.n, lon: a.lon / a.n, ok: a.ok, n: a.n,
               warn: a.fc / a.ok >= 0.5 ? Math.round((100 * a.fc) / a.ok) : 0 };
    }).sort((x, y) => y.score - x.score);
    const top = rows.slice(0, 150);
    $("rank").querySelector("tbody").innerHTML = top.map((r) => {
      const tr = r.tr == null ? '<span class="d">—</span>' : `<span class="${r.tr > 5 ? "up" : r.tr < -5 ? "down" : ""}">${r.tr > 0 ? "↑" : r.tr < 0 ? "↓" : ""}${Math.abs(r.tr).toFixed(0)}%</span>`;
      const warn = r.warn ? ` <span class="warn-mark" title="Dados de crime incertos em ${r.warn}% das quadras (subnotificação provável ou poucos registros)">⚠</span>` : "";
      return `<tr data-lat="${r.lat}" data-lon="${r.lon}" title="${esc(r.name)} · ${r.ok} de ${r.n} quadras passam nos filtros">
        <td>${esc(r.name)}${warn}<br><span class="d">${esc(r.d || "")}</span></td><td><b>${r.score.toFixed(0)}</b></td><td>${r.t == null ? "—" : Math.round(r.t)}</td><td>${Math.round(r.rob)}</td><td>${tr}</td></tr>`;
    }).join("") || `<tr><td colspan="5" class="d">Nenhum bairro passa nos filtros. Aumente o tempo máximo.</td></tr>`;
    $("rank").querySelectorAll("tbody tr[data-lat]").forEach((tr) => tr.addEventListener("click", () => {
      map.setView([+tr.dataset.lat, +tr.dataset.lon], 15);
      if (isPhone()) window.scrollTo({ top: 0, behavior: "smooth" });
    }));
    $("rank-note").textContent = rows.length > top.length ? `(top ${top.length} de ${rows.length})` : `(${rows.length} bairros)`;
  }

  let queued = false;
  function scheduleUpdate() {
    if (queued) return;
    queued = true;
    const run = () => { if (!queued) return; queued = false; update(); };
    requestAnimationFrame(run);
    setTimeout(run, 50);
  }
  function update() {
    if (!D) return;
    $("maxc-out").textContent = S.dest ? `${S.maxc} min` : "—";
    $("metrod-out").textContent = S.metroD;
    $("scenario-hint").textContent = S.scenario === "now"
      ? "Linha 6 fora do cálculo: hoje opera só em dias úteis, das 10h às 15h, com trem a cada ~20 min (6 estações)."
      : "Simula a Linha 6 inteira (Brasilândia–São Joaquim) operando em horário normal.";
    const opt = $("layer").querySelector('option[value="commute"]');
    if (opt) opt.disabled = !S.dest;
    if (currentLayer().needsDest && !S.times) { S.layer = "score"; $("layer").value = "score"; }
    recompute();
    classify();
    paint();
    legend();
    ranking();
    Imoveis.render();
  }

  // ---------------------------------------------------------------- Meus imóveis
  const Imoveis = (() => {
    const KEY = "omsp.imoveis.v1";
    const layer = L.layerGroup();
    let list = [];
    let editing = null;     // id being edited, or null for a new one
    let draftLoc = null;    // {lat, lon} chosen in the form

    const num = (v, max = 1e7) => { const n = Number(v); return isFinite(n) && n >= 0 && n <= max ? n : null; };
    const str = (v, max) => (v == null ? "" : String(v).slice(0, max));
    function clean(o) {  // shape + validate anything coming from storage, files or shared links
      const lat = Number(o.lat), lon = Number(o.lon);
      if (!isFinite(lat) || !isFinite(lon) || !inCity(lat, lon)) return null;
      const aluguel = num(o.aluguel);
      if (aluguel == null) return null;
      return {
        id: str(o.id, 40) || Math.random().toString(36).slice(2, 12),
        url: safeUrl(o.url) || "", nome: str(o.nome, 60), end: str(o.end, 120), lat, lon,
        aluguel, cond: num(o.cond) || 0, iptu: num(o.iptu) || 0,
        quartos: num(o.quartos, 20), vagas: num(o.vagas, 20), area: num(o.area, 100000),
        pet: ["sim", "nao"].includes(o.pet) ? o.pet : "", mob: ["sim", "semi", "nao"].includes(o.mob) ? o.mob : "",
        obs: str(o.obs, 300),
      };
    }
    const total = (m) => m.aluguel + (m.cond || 0) + (m.iptu || 0);
    const load = () => { try { list = (JSON.parse(localStorage.getItem(KEY)) || []).map(clean).filter(Boolean); } catch { list = []; } };
    const save = () => { try { localStorage.setItem(KEY, JSON.stringify(list)); } catch { alert("Não foi possível salvar neste navegador."); } };

    function passes(m) {
      const f = (id) => { const v = $(id).value; return v === "" ? null : Number(v); };
      const t = total(m);
      if (f("f-min") != null && t < f("f-min")) return false;
      if (f("f-max") != null && t > f("f-max")) return false;
      if (f("f-quartos") != null && (m.quartos ?? -1) < f("f-quartos")) return false;
      if (f("f-vagas") != null && (m.vagas ?? -1) < f("f-vagas")) return false;
      if (f("f-area") != null && (m.area ?? -1) < f("f-area")) return false;
      if ($("f-pet").checked && m.pet !== "sim") return false;
      if ($("f-mob").value && m.mob !== $("f-mob").value) return false;
      return true;
    }
    function cellOf(m) { return D ? cellAt(L.latLng(m.lat, m.lon)) : -1; }
    function minutes(m) { const c = cellOf(m); return c >= 0 && S.times ? S.times[c] : null; }

    function render() {
      if (!D) return;
      layer.clearLayers();
      const items = list.map((m) => ({ m, ok: passes(m) }));
      items.filter((x) => x.ok).forEach(({ m }) => {
        const icon = L.divIcon({ className: "", html: `<div class="im-pin">${esc(brlShort(total(m)))}</div>`, iconSize: [0, 0] });
        L.marker([m.lat, m.lon], { icon, pane: "pins", keyboard: false, title: m.nome || m.end })
          .on("click", () => show(m.id)).addTo(layer);
      });
      const shown = items.filter((x) => x.ok).length;
      $("imoveis-note").textContent = list.length ? `(${shown} de ${list.length} no mapa)` : "";
      $("im-list").innerHTML = items.length ? items.map(({ m, ok }) => {
        const t = minutes(m);
        const bits = [m.quartos != null ? `${m.quartos} qto` : null, m.vagas != null ? `${m.vagas} vaga${m.vagas === 1 ? "" : "s"}` : null,
          m.area ? `${m.area} m²` : null, m.pet === "sim" ? "aceita pet" : m.pet === "nao" ? "sem pet" : null,
          t != null ? `${Math.round(t)} min até ${S.dest.name}` : null].filter(Boolean).map(esc).join(" · ");
        return `<li data-id="${esc(m.id)}" class="${ok ? "" : "out"}"><div class="top"><b>${esc(m.nome || m.end || "Imóvel")}</b><span class="price">${esc(brl(total(m)))}</span></div><div class="d">${bits}${ok ? "" : " · fora do filtro"}</div></li>`;
      }).join("") : `<li class="empty">Nenhum imóvel ainda.</li>`;
      $("im-list").querySelectorAll("li[data-id]").forEach((li) => li.addEventListener("click", () => {
        const m = list.find((x) => x.id === li.dataset.id);
        if (!m) return;
        map.setView([m.lat, m.lon], Math.max(map.getZoom(), 15));
        if (isPhone()) window.scrollTo({ top: 0, behavior: "smooth" });
        show(m.id);
      }));
    }

    function show(id) {
      const m = list.find((x) => x.id === id);
      if (!m) return;
      const c = cellOf(m);
      const url = safeUrl(m.url);
      const facts = [
        ["Quartos", m.quartos], ["Vagas", m.vagas], ["Área", m.area ? `${m.area} m²` : null],
        ["Aceita pet", m.pet === "sim" ? "Sim" : m.pet === "nao" ? "Não" : null],
        ["Mobília", { sim: "Mobiliado", semi: "Semimobiliado", nao: "Sem mobília" }[m.mob] || null],
        ["Endereço", m.end || null], ["Observações", m.obs || null],
      ].filter(([, v]) => v != null && v !== "").map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("");
      const html = `<div class="pop">
        <h3>${esc(m.nome || m.end || "Imóvel")}</h3>
        <div class="cost"><b>${esc(brl(total(m)))}</b> por mês <span class="d">= aluguel ${esc(brl(m.aluguel))} + condomínio ${esc(brl(m.cond || 0))} + IPTU ${esc(brl(m.iptu || 0))}</span></div>
        <table>${facts}</table>
        <div class="actions">${url ? `<a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Abrir anúncio ↗</a>` : ""}<button type="button" class="btn" data-edit="${esc(m.id)}">Editar</button></div>
        ${c >= 0 ? `<h3 style="margin-top:12px">A região: ${esc(bairroName(c))}</h3>${cellSummary(c)}` : ""}
      </div>`;
      select(c);
      openDetails(html, L.latLng(m.lat, m.lon));
      if (c >= 0) fillRoute(c);
    }
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-edit]");
      if (b) { $("sheet").hidden = true; openForm(b.dataset.edit); }
    });

    // ---- form
    const dlg = $("im-dialog"), form = $("im-form");
    function setLoc(lat, lon, label) {
      draftLoc = { lat, lon };
      $("im-loc").textContent = label ? `📍 ${label}` : `📍 ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    }
    function openForm(id) {
      editing = id || null;
      const m = id ? list.find((x) => x.id === id) : null;
      form.reset();
      draftLoc = null;
      $("im-loc").textContent = "Nenhum local escolhido";
      $("im-error").hidden = true;
      $("im-title").textContent = m ? "Editar imóvel" : "Adicionar imóvel";
      $("im-delete").hidden = !m;
      if (m) {
        for (const k of ["url", "nome", "end", "aluguel", "cond", "iptu", "quartos", "vagas", "area", "pet", "mob", "obs"]) {
          if (form.elements[k]) form.elements[k].value = m[k] ?? "";
        }
        setLoc(m.lat, m.lon, m.end);
      }
      dlg.showModal();
    }
    function readForm() {
      const v = (k) => form.elements[k].value.trim();
      const n = (k) => (v(k) === "" ? null : Number(v(k)));
      return { id: editing, url: v("url"), nome: v("nome"), end: v("end"), lat: draftLoc && draftLoc.lat, lon: draftLoc && draftLoc.lon,
        aluguel: n("aluguel"), cond: n("cond"), iptu: n("iptu"), quartos: n("quartos"), vagas: n("vagas"), area: n("area"),
        pet: v("pet"), mob: v("mob"), obs: v("obs") };
    }
    let draftSnapshot = null;
    $("im-pick").addEventListener("click", () => {
      draftSnapshot = readForm();
      dlg.close();
      startPicking(async (ll) => {
        reopenWith(draftSnapshot);
        setLoc(ll.lat, ll.lng);
        if (!form.elements.end.value) {
          const name = await reverseGeocode(ll.lat, ll.lng);
          if (name) { form.elements.end.value = name; setLoc(ll.lat, ll.lng, name); }
        }
      }, "Toque no mapa onde fica o imóvel");
    });
    function reopenWith(d) {
      for (const k of ["url", "nome", "end", "aluguel", "cond", "iptu", "quartos", "vagas", "area", "pet", "mob", "obs"]) form.elements[k].value = d[k] ?? "";
      dlg.showModal();
    }
    searchBox(form.elements.end, $("im-results"), (r) => { form.elements.end.value = [r.main, r.sub].filter(Boolean).join(" · ").slice(0, 120); setLoc(r.lat, r.lon, r.main); });
    $("im-cancel").addEventListener("click", () => dlg.close());
    $("im-delete").addEventListener("click", () => {
      if (!editing || !confirm("Excluir este imóvel?")) return;
      list = list.filter((x) => x.id !== editing);
      save(); render(); dlg.close();
    });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const raw = readForm();
      const err = (t) => { $("im-error").textContent = t; $("im-error").hidden = false; };
      if (raw.aluguel == null || !(raw.aluguel >= 0)) return err("Informe o valor do aluguel.");
      if (!draftLoc) return err("Escolha a localização: busque o endereço ou marque no mapa.");
      if (raw.url && !safeUrl(raw.url)) return err("O link precisa começar com http:// ou https://");
      const m = clean(raw);
      if (!m) return err("A localização precisa estar dentro da cidade de São Paulo.");
      if (editing) list = list.map((x) => (x.id === editing ? { ...m, id: editing } : x));
      else list.push(m);
      save(); render(); dlg.close();
      map.setView([m.lat, m.lon], Math.max(map.getZoom(), 15));
    });

    // ---- share / export / import
    const b64 = { enc: (s) => btoa(String.fromCharCode(...new TextEncoder().encode(s))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
                  dec: (s) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (ch) => ch.charCodeAt(0))) };
    function merge(items) {
      let added = 0;
      for (const raw of items) {
        const m = clean(raw);
        if (!m) continue;
        const dup = list.some((x) => x.id === m.id || (m.url && x.url === m.url && x.aluguel === m.aluguel));
        if (!dup) { list.push(m); added++; }
      }
      save(); render();
      return added;
    }
    $("im-share").addEventListener("click", async () => {
      if (!list.length) { alert("Adicione pelo menos um imóvel."); return; }
      const slim = list.map(({ id, ...rest }) => ({ id, ...Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== "" && v != null)) }));
      const url = location.origin + location.pathname + location.search + "#imoveis=" + b64.enc(JSON.stringify(slim));
      try { await navigator.clipboard.writeText(url); flash($("im-share"), "Link copiado!"); }
      catch { prompt("Copie o link:", url); }
    });
    $("im-export").addEventListener("click", () => {
      const blob = new Blob([JSON.stringify(list, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = "meus-imoveis.json"; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
    $("im-import").addEventListener("change", async (e) => {
      const f = e.target.files[0]; e.target.value = "";
      if (!f) return;
      try { const n = merge(JSON.parse(await f.text())); alert(`${n} imóvel(is) importado(s).`); }
      catch { alert("Arquivo inválido."); }
    });
    ["f-min", "f-max", "f-quartos", "f-vagas", "f-area", "f-mob", "f-pet"].forEach((id) => $(id).addEventListener("input", render));

    function init() {
      load();
      $("im-add").addEventListener("click", () => openForm(null));
      const h = location.hash.match(/imoveis=([A-Za-z0-9_-]+)/);
      if (h) {
        try {
          const items = JSON.parse(b64.dec(h[1]));
          if (Array.isArray(items) && confirm(`Este link traz ${items.length} imóvel(is) compartilhado(s). Adicionar à sua lista?`)) {
            const n = merge(items);
            alert(`${n} imóvel(is) adicionado(s).`);
          }
        } catch { /* ignore a broken link */ }
        history.replaceState(null, "", location.pathname + location.search);
      }
      render();
    }
    return { init, render, layer };
  })();
})();

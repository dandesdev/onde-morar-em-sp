/* Onde Morar Em SP: routing worker.
 * Loads the transit graph (data/graph.bin.gz, layout in data/graph.json) and answers:
 *   {type:"init", cells:{i,j,grid}}                        -> {type:"ready"}
 *   {type:"route", id, dest:{lon,lat}, scenario:"now"|"l6"} -> {type:"times", id, times:Float32Array}
 *   {type:"path", id, cell}                                -> {type:"path", id, path}
 * Times are door-to-door minutes from each grid cell to the destination (see scripts/build_site.py).
 */
"use strict";

const MAX_DEST_WALK = 1500; // m of straight line walked from the last stop to the destination

let G = null;      // graph arrays
let C = null;      // cell centres + nearest hubs
let L6 = null;     // reverse adjacency of the Line 6 edges
let last = null;   // state of the last route, for path queries

async function fetchMaybeGzip(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  const buf = new Uint8Array(await res.arrayBuffer());
  if (buf[0] !== 0x1f || buf[1] !== 0x8b) return buf.buffer; // server already decompressed it
  const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
  return await new Response(stream).arrayBuffer();
}

function view(buf, sec) {
  const T = { "<f4": Float32Array, "|u1": Uint8Array, "<u2": Uint16Array, "<u4": Uint32Array }[sec.type];
  return new T(buf, sec.offset, sec.length);
}

async function init(cells) {
  const meta = await (await fetch("data/graph.json")).json();
  const buf = await fetchMaybeGzip("data/graph.bin.gz");
  const s = meta.sections;
  G = {
    meta, n: meta.nodes, walk: meta.walk_m_per_min,
    lon: view(buf, s.lon), lat: view(buf, s.lat), kind: view(buf, s.kind), label: view(buf, s.label),
    roff: view(buf, s.roff), rsrc: view(buf, s.rsrc), rw: view(buf, s.rw),
    cellHub: view(buf, s.cellHub), cellWalk: view(buf, s.cellWalk),
  };
  // Line 6: reverse CSR keyed by the edge's target
  const from = view(buf, s.l6from), to = view(buf, s.l6to), w = view(buf, s.l6w);
  const off = new Uint32Array(G.n + 1);
  for (let k = 0; k < to.length; k++) off[to[k] + 1]++;
  for (let v = 0; v < G.n; v++) off[v + 1] += off[v];
  const src = new Uint32Array(to.length), ww = new Uint16Array(to.length), fill = off.slice(0, G.n);
  for (let k = 0; k < to.length; k++) { const p = fill[to[k]]++; src[p] = from[k]; ww[p] = w[k]; }
  L6 = { off, src, w: ww };
  const hubs = [];
  for (let v = 0; v < G.n; v++) if (G.kind[v] === 0) hubs.push(v);
  G.hubs = Uint32Array.from(hubs);

  const g = cells.grid;
  const n = cells.i.length;
  C = { n, lon: new Float64Array(n), lat: new Float64Array(n), k: meta.k };
  for (let c = 0; c < n; c++) {
    C.lon[c] = g.lon0 + (cells.i[c] + 0.5) * g.dlon;
    C.lat[c] = g.lat0 + (cells.j[c] + 0.5) * g.dlat;
  }
}

function metres(lon1, lat1, lon2, lat2) {
  const kx = 111320 * Math.cos(((lat1 + lat2) / 2) * Math.PI / 180);
  return Math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * 110574);
}

// Binary min-heap with lazy deletion.
class Heap {
  constructor(cap) { this.k = new Float64Array(cap); this.v = new Int32Array(cap); this.n = 0; }
  push(key, val) {
    if (this.n === this.k.length) { const k = new Float64Array(this.n * 2), v = new Int32Array(this.n * 2); k.set(this.k); v.set(this.v); this.k = k; this.v = v; }
    let i = this.n++;
    while (i > 0) { const p = (i - 1) >> 1; if (this.k[p] <= key) break; this.k[i] = this.k[p]; this.v[i] = this.v[p]; i = p; }
    this.k[i] = key; this.v[i] = val;
  }
  pop() { // returns value; key in this.top
    const val = this.v[0]; this.top = this.k[0];
    const key = this.k[--this.n], v = this.v[this.n];
    let i = 0;
    for (;;) {
      let c = 2 * i + 1; if (c >= this.n) break;
      if (c + 1 < this.n && this.k[c + 1] < this.k[c]) c++;
      if (this.k[c] >= key) break;
      this.k[i] = this.k[c]; this.v[i] = this.v[c]; i = c;
    }
    this.k[i] = key; this.v[i] = v;
    return val;
  }
}

function route(dest, scenario) {
  const n = G.n;
  // Float64 on purpose: with Float32 the stored value can round below the heap key and the
  // "stale entry" check would then skip the node for good.
  const dist = new Float64Array(n).fill(Infinity);
  const next = new Int32Array(n).fill(-1); // next node towards the destination; -2 = walk to destination
  const heap = new Heap(1 << 16);
  for (const h of G.hubs) {
    const d = metres(G.lon[h], G.lat[h], dest.lon, dest.lat);
    if (d <= MAX_DEST_WALK) { dist[h] = d / G.walk; next[h] = -2; heap.push(dist[h], h); }
  }
  const useL6 = scenario === "l6";
  while (heap.n) {
    const v = heap.pop(), dv = heap.top;
    if (dv > dist[v]) continue;
    for (let k = G.roff[v], e = G.roff[v + 1]; k < e; k++) {
      const u = G.rsrc[k], du = dv + G.rw[k] / 10;
      if (du < dist[u]) { dist[u] = du; next[u] = v; heap.push(du, u); }
    }
    if (useL6) {
      for (let k = L6.off[v], e = L6.off[v + 1]; k < e; k++) {
        const u = L6.src[k], du = dv + L6.w[k] / 10;
        if (du < dist[u]) { dist[u] = du; next[u] = v; heap.push(du, u); }
      }
    }
  }
  // cells: walk to one of their nearest hubs, or walk straight to the destination
  const times = new Float32Array(C.n), best = new Int32Array(C.n), K = C.k;
  for (let c = 0; c < C.n; c++) {
    let t = metres(C.lon[c], C.lat[c], dest.lon, dest.lat) / G.walk, b = -1;
    for (let k = 0; k < K; k++) {
      const w = G.cellWalk[c * K + k];
      if (w === 255) break;
      const h = G.cellHub[c * K + k], tt = w / 4 + dist[h];
      if (tt < t) { t = tt; b = h; }
    }
    times[c] = t; best[c] = b;
  }
  last = { dist, next, best, dest };
  return times;
}

function path(c) {
  if (!last) return null;
  const { dist, next, best, dest } = last;
  const h = best[c];
  if (h < 0) {
    const w = metres(C.lon[c], C.lat[c], dest.lon, dest.lat) / G.walk;
    return { total: w, walk: w, wait: 0, legs: [] };
  }
  const K = C.k;
  let walk = 0;
  for (let k = 0; k < K; k++) if (G.cellHub[c * K + k] === h) { walk = G.cellWalk[c * K + k] / 4; break; }
  const total = walk + dist[h];
  let wait = 0;
  const legs = [];
  let u = h, guard = 0;
  while (guard++ < 5000) {
    const v = next[u];
    if (v === -2) { walk += dist[u]; break; }
    if (v < 0) break;
    const w = dist[u] - dist[v];
    const ku = G.kind[u], kv = G.kind[v];
    if (ku === 0 && kv === 0) walk += w;
    else if (ku === 0 && kv > 0) {
      wait += w;
      const lab = G.meta.labels[G.label[v]];
      legs.push({ t: lab.t, c: lab.c, n: lab.n, col: lab.col, from: lab.n, to: lab.n, ride: 0 });
    } else if (ku > 0 && kv > 0) {
      const leg = legs[legs.length - 1];
      if (leg) { leg.ride += w; if (G.kind[v] === 2) leg.to = G.meta.labels[G.label[v]].n; }
    } else if (ku > 0 && kv === 0) wait += w; // platform exit time
    u = v;
  }
  return { total, walk, wait, legs };
}

self.onmessage = async (e) => {
  const m = e.data;
  try {
    if (m.type === "init") { await init(m.cells); self.postMessage({ type: "ready" }); }
    else if (m.type === "route") {
      const t0 = performance.now();
      const times = route(m.dest, m.scenario);
      self.postMessage({ type: "times", id: m.id, times, ms: Math.round(performance.now() - t0) }, [times.buffer]);
    } else if (m.type === "path") self.postMessage({ type: "path", id: m.id, path: path(m.cell) });
  } catch (err) {
    self.postMessage({ type: "error", id: m.id, message: String(err && err.message || err) });
  }
};

// Runs router.js under Node against the built data and prints commute times to a destination.
// Usage: node pipeline/test_router.mjs [lat,lon] [--check]
//   data folder: $OMSP_OUT (default ./data); default destination: CGE-SP (Rua Barão de Tefé, 72)
//   --check: exit 1 if known commutes look broken (used by the weekly update before publishing)
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = resolve(fileURLToPath(import.meta.url), "../..");
const DATA = resolve(process.env.OMSP_OUT || resolve(ROOT, "data"));
const readData = (name) => readFileSync(resolve(DATA, name));
const replies = [];
const sandbox = {
  self: { postMessage: (m) => replies.push(m) },
  fetch: async (url) => {
    const buf = readData(String(url).replace(/^data\//, ""));
    return { ok: true, json: async () => JSON.parse(buf), arrayBuffer: async () => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) };
  },
  __gunzip: (b) => gunzipSync(Buffer.from(b)),
  performance, console, Math, Float32Array, Float64Array, Int32Array, Uint8Array, Uint16Array, Uint32Array, Infinity,
};
// the browser unzips with DecompressionStream; Node uses zlib
const src = readFileSync(resolve(ROOT, "router.js"), "utf8").replace(
  /const stream = new Blob\(\[buf\]\)[\s\S]*?return await new Response\(stream\)\.arrayBuffer\(\);/,
  "const out = __gunzip(buf); return out.buffer.slice(out.byteOffset, out.byteOffset + out.byteLength);");
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const args = process.argv.slice(2);
const check = args.includes("--check");
const [dlat, dlon] = (args.find((a) => /^-?\d/.test(a)) || "-23.5275,-46.6760").split(",").map(Number);
const cells = JSON.parse(gunzipSync(readData("cells.json.gz")));
const call = async (msg) => { replies.length = 0; await sandbox.self.onmessage({ data: msg }); return replies[0]; };
const init = await call({ type: "init", cells: { i: cells.i, j: cells.j, grid: cells.grid } });
if (init.type !== "ready") { console.error(init); process.exit(1); }

const g = cells.grid;
const index = new Map(cells.i.map((i, c) => [`${i},${cells.j[c]}`, c]));
const cellOf = (lat, lon) => index.get(`${Math.floor((lon - g.lon0) / g.dlon)},${Math.floor((lat - g.lat0) / g.dlat)}`) ?? -1;
// [name, lat, lon, max plausible minutes to CGE today]
const spots = [
  ["Pompeia (Sesc)", -23.5260, -46.6845, 25], ["Perdizes (PUC)", -23.5370, -46.6700, 30], ["Alto da Lapa", -23.5395, -46.7155, 50],
  ["Higienópolis", -23.5458, -46.6590, 45], ["Metrô Vila Madalena", -23.5462, -46.6913, 40], ["Santa Cecília", -23.5390, -46.6497, 35],
  ["Casa Verde", -23.5100, -46.6560, 50], ["Vila Romana", -23.5300, -46.6960, 40], ["Tatuapé (metrô)", -23.5403, -46.5762, 55],
  ["Santo Amaro (Largo 13)", -23.6544, -46.7092, 95],
];
let failed = 0;
for (const sc of ["now", "l6"]) {
  const r = await call({ type: "route", id: 1, dest: { lat: dlat, lon: dlon }, scenario: sc });
  const reach = r.times.filter((t) => t <= 45).length;
  console.log(`\n== ${sc}: ${r.ms} ms, cells <=45 min: ${reach} of ${r.times.length}`);
  if (check && reach < 800) { console.error(`only ${reach} cells within 45 min`); failed++; }
  for (const [name, lat, lon, max] of spots) {
    const c = cellOf(lat, lon);
    const p = (await call({ type: "path", id: 2, cell: c })).path;
    const legs = p.legs.map((l) => (l.t === "r" ? `${l.c} ${l.from}→${l.to}` : l.c)).join(" > ");
    const t = r.times[c];
    const bad = check && sc === "now" && !(t <= max);
    if (bad) failed++;
    console.log(`${bad ? "FAIL " : ""}${name.padEnd(26)} ${t.toFixed(0).padStart(3)} min  ${legs || "a pé"}`);
  }
}
if (failed) { console.error(`\n${failed} routing check(s) failed`); process.exit(1); }

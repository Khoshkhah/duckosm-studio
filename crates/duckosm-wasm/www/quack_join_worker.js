// The join of quack by tiles in a Web Worker of its own, so it has its own pool of threads (the setting "join" of the
// page; the cut and the tile workers have their own settings). In: {dir: the folder of the run in OPFS, parts: the names
// of the parts of the tiles, sort: true or false (true: the parts are sorted, and the join merges them), threads, gdal:
// GDAL's layers}. It writes quack.parquet (gdal: one GeoParquet file for each layer, <layer>.parquet). Out: {merged, total}
// (the rows of the merge, while it runs), {done: i} (all rows of part i are read: its file can go), then {rows, ms, mib:
// the memory of this worker} (gdal: also {layers: the rows of each layer}) or {error}.

// the temporary files of the sort (a strip of QuackOSM's order that is larger than the budget; gdal: the members of the
// relations and the sort of them)
const SPILL = 8;
const SPILL_GDAL = 64;
// GDAL's layers, in their order
const LAYERS = ["points", "lines", "multilinestrings", "multipolygons", "other_relations"];

onmessage = async ({ data }) => {
  const spill = [];
  let out = null;
  const outs = [];
  try {
    const isolated = self.crossOriginIsolated;
    const pkg = await import(isolated ? "./pkg-threads/duckosm_wasm.js" : "./pkg/duckosm_wasm.js");
    const wasm = await pkg.default();
    if (isolated) await pkg.initThreadPool(Math.max(1, data.threads));
    const files = [];
    for (const name of data.parts) files.push(await (await data.dir.getFileHandle(name)).getFile());
    if (data.sort || data.gdal) {
      // GDAL's layers: the members of the relations wait in a file, and their sort needs a file for each run (France: 8
      // were not enough)
      const n = data.gdal ? SPILL_GDAL : SPILL;
      for (let i = 0; i < n; i++) spill.push(await (await data.dir.getFileHandle(`j${i}`, { create: true })).createSyncAccessHandle());
    }
    if (data.gdal) {
      for (const l of LAYERS) outs.push(await (await data.dir.getFileHandle(`${l}.parquet`, { create: true })).createSyncAccessHandle());
      const t = performance.now();
      const layers = pkg.quack_gdal_join(files, spill, outs);
      for (const h of outs) h.flush();
      const rows = LAYERS.reduce((a, l) => a + layers[l], 0);
      postMessage({ rows, layers, ms: performance.now() - t, mib: Math.round(wasm.memory.buffer.byteLength / 2 ** 20) });
      return;
    }
    out = await (await data.dir.getFileHandle("quack.parquet", { create: true })).createSyncAccessHandle();
    const t = performance.now();
    const rows = pkg.quack_join(files, data.sort, spill, out);
    out.flush();
    // the WebAssembly memory of the join (it never shrinks: the most that the join used)
    postMessage({ rows, ms: performance.now() - t, mib: Math.round(wasm.memory.buffer.byteLength / 2 ** 20) });
  } catch (e) {
    postMessage({ error: String((e && e.message) || e) });
  } finally {
    if (out) out.close();
    for (const h of outs) h.close();
    for (const h of spill) h.close();
    for (let i = 0; i < spill.length; i++) await data.dir.removeEntry(`j${i}`).catch(() => {});
  }
};

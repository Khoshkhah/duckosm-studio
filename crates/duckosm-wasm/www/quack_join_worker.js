// The join of quack by tiles in a Web Worker of its own, so it has its own pool of threads (the setting "join" of the
// page; the cut and the tile workers have their own settings). In: {dir: the folder of the run in OPFS, parts: the names
// of the parts of the tiles, sort: true or false (true: the parts are sorted, and the join merges them), threads}. It
// writes quack.parquet. Out: {merged, total} (the rows of the merge, while it runs), then {rows, ms, mib: the memory of
// this worker} or {error}.

// the temporary files of the sort (a strip of QuackOSM's order that is larger than the budget)
const SPILL = 8;

onmessage = async ({ data }) => {
  const spill = [];
  let out = null;
  try {
    const isolated = self.crossOriginIsolated;
    const pkg = await import(isolated ? "./pkg-threads/duckosm_wasm.js" : "./pkg/duckosm_wasm.js");
    const wasm = await pkg.default();
    if (isolated) await pkg.initThreadPool(Math.max(1, data.threads));
    const files = [];
    for (const name of data.parts) files.push(await (await data.dir.getFileHandle(name)).getFile());
    if (data.sort) {
      for (let i = 0; i < SPILL; i++) spill.push(await (await data.dir.getFileHandle(`j${i}`, { create: true })).createSyncAccessHandle());
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
    for (const h of spill) h.close();
    for (let i = 0; i < spill.length; i++) await data.dir.removeEntry(`j${i}`).catch(() => {});
  }
};

// duckOSM's quack in a Web Worker (docs/rust/quack.md of duckOSM): the features table of QuackOSM (feature_id, tags,
// geometry) of a .osm.pbf file, made by the Rust engine in WebAssembly.
// In: {pbf: File, sort: true or false (default true: the order of QuackOSM)}. Out: {nodes, ways, relations, steps: for
// each step its time since the start (ms) and the WebAssembly memory (MiB), file: the GeoParquet file (a File in OPFS),
// seconds, threads, mib} or {error}. The page makes a new worker for each run: the
// memory of a WebAssembly module never shrinks. A cross-origin isolated page gets the module with threads
// (pkg-threads), another page the module with one thread (pkg), as in build_worker.js.
//
// The temporary files and the GeoParquet file are in OPFS, not in the memory of the tab. A file of OPFS opens only
// asynchronously, so the worker opens them here, before the synchronous run, in a folder of its own. The folder keeps
// the GeoParquet file for the download; the next run removes it.

let pkg = null, wasm = null, threads = 1;

// ponytail: 192 temporary files; the node buckets (2^27 ids each) and the point buckets of a run can be about 170 at once
const SPILL = 192;

// The threads of the pool are Workers that wasm-bindgen-rayon starts from this worker. An error in one of them (no memory
// left gives "unreachable") comes only to its Worker object, and the run then waits for ever for that thread. So each
// Worker that this worker starts sends its error to the page, which then ends the run.
const NativeWorker = self.Worker;
self.Worker = class extends NativeWorker {
  constructor(...args) {
    super(...args);
    this.addEventListener("error", (e) => {
      postMessage({ error: `a thread of the WebAssembly module stopped (${e.message || "error"}): most likely the 4 GiB ` +
        "of memory of the browser are not enough for this file. Clear the sort, or use duckosm-rust quack on a computer." });
    });
  }
};

onmessage = async ({ data }) => {
  const root = await navigator.storage.getDirectory();
  // ponytail: removes the folders of all earlier runs, also of a run in another tab
  for await (const name of root.keys()) {
    if (name.startsWith("quack-")) await root.removeEntry(name, { recursive: true }).catch(() => {});
  }
  const dir = await root.getDirectoryHandle(`quack-${Date.now()}`, { create: true });
  const spill = [];
  let out = null;
  try {
    if (!pkg) {
      const isolated = self.crossOriginIsolated;
      pkg = await import(isolated ? "./pkg-threads/duckosm_wasm.js" : "./pkg/duckosm_wasm.js");
      wasm = await pkg.default();
      if (isolated) {
        threads = Math.min(navigator.hardwareConcurrency || 4, 12);
        await pkg.initThreadPool(threads);
      }
    }
    for (let i = 0; i < SPILL; i++) spill.push(await (await dir.getFileHandle(`t${i}`, { create: true })).createSyncAccessHandle());
    const fh = await dir.getFileHandle("quack.parquet", { create: true });
    out = await fh.createSyncAccessHandle();
    const t = performance.now();
    const r = pkg.quack(data.pbf, data.sort !== false, spill, out);
    const seconds = (performance.now() - t) / 1000;
    const mib = Math.round(wasm.memory.buffer.byteLength / 2 ** 20);   // the WebAssembly memory at the end (it never shrinks)
    out.close();
    out = null;
    postMessage({ nodes: r.nodes, ways: r.ways, relations: r.relations, steps: r.steps, file: await fh.getFile(), seconds, threads, mib });
  } catch (e) {
    postMessage({ error: String((e && e.message) || e) });
  } finally {
    for (const h of spill) h.close();
    if (out) out.close();
    for (let i = 0; i < SPILL; i++) await dir.removeEntry(`t${i}`).catch(() => {});
  }
};

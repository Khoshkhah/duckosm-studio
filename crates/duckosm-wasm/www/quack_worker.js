// duckOSM's quack in a Web Worker (docs/rust/quack.md of duckOSM): the features table of QuackOSM (feature_id, tags,
// geometry) of a .osm.pbf file, made by the Rust engine in WebAssembly.
// In: {pbf: File, sort: true or false (default true: the order of QuackOSM)}. Out: {nodes, ways, relations, parquet:
// Uint8Array (transferred, not copied), seconds, threads, mib} or {error}. The page makes a new worker for each run:
// the memory of a WebAssembly module never shrinks. A cross-origin isolated page gets the module
// with threads (pkg-threads), another page the module with one thread (pkg), as in build_worker.js.

let pkg = null, wasm = null, threads = 1;

onmessage = async ({ data }) => {
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
    const t = performance.now();
    const r = pkg.quack(data.pbf, data.sort !== false);
    const seconds = (performance.now() - t) / 1000;
    const mib = Math.round(wasm.memory.buffer.byteLength / 2 ** 20);   // the WebAssembly memory at the end (it never shrinks)
    postMessage({ nodes: r.nodes, ways: r.ways, relations: r.relations, parquet: r.parquet, seconds, threads, mib },
      [r.parquet.buffer]);
  } catch (e) {
    postMessage({ error: String((e && e.message) || e) });
  }
};

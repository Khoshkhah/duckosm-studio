// One tile of quack by tiles in a Web Worker of its own (quack_worker.js starts one for each core). Each worker has its
// own WebAssembly module (with one thread) and its own memory, and opens its own files in OPFS, so the tiles run at the
// same time with no wait for each other.
// In: {dir: the folder of the run in OPFS, i: the number of the tile, tile: [z, x, y], module: the compiled module}. The worker reads tile-<i>.pbf
// and writes part-<i>.parquet: the rows that the tile owns. Out: {i, nodes, ways, relations, ms, mib: the memory of this
// worker} or {i, error}.

let pkg = null, wasm = null;

onmessage = async ({ data }) => {
  try {
    if (!pkg) {
      pkg = await import("./pkg/duckosm_wasm.js");
      // the module that quack_worker.js compiled one time for all the tile workers
      wasm = await pkg.default({ module_or_path: data.module });
    }
    const file = await (await data.dir.getFileHandle(`tile-${data.i}.pbf`)).getFile();
    const out = await (await data.dir.getFileHandle(`part-${data.i}.parquet`, { create: true })).createSyncAccessHandle();
    const t = performance.now();
    try {
      const r = pkg.quack_tile(file, data.tile[0], data.tile[1], data.tile[2], out);
      out.flush();
      // the memory of this worker (each worker has its own WebAssembly memory; it never shrinks)
      const mib = Math.round(wasm.memory.buffer.byteLength / 2 ** 20);
      // begin: when this tile began, after the start of the worker (a time that all workers share)
      postMessage({ i: data.i, nodes: r.nodes, ways: r.ways, relations: r.relations, ms: performance.now() - t, mib, begin: performance.timeOrigin + t });
    } finally {
      out.close();
    }
  } catch (e) {
    postMessage({ i: data.i, error: String((e && e.message) || e) });
  }
};

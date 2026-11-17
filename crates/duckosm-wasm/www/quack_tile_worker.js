// One tile of quack by tiles in a Web Worker of its own (quack_worker.js starts one for each thread). Each worker has its
// own WebAssembly module and its own memory, and opens its own files in OPFS, so the tiles run at the same time with no
// wait for each other. A worker of a large tile has the module with threads and a pool of its own.
// In: {dir: the folder of the run in OPFS, i: the number of the tile, tile: [z, x, y], module: the compiled module
// (one thread), threads: the threads of this worker (the first message sets them), sort: a part sorted for the merge
// of the join}. The worker reads tile-<i>.pbf
// and writes part-<i>.parquet: the rows that the tile owns. Out: {i, nodes, ways, relations, ms, mib: the memory of this
// worker} or {i, error}.

let pkg = null, wasm = null, spill = null;
// the temporary files of a tile (the buckets of the nodes and of the points: about 100 each for France), as in one run
const SPILL = 192;

onmessage = async ({ data }) => {
  try {
    if (!pkg && data.threads > 1) {
      // a large tile: the module with threads and a pool of `threads` threads for this worker
      pkg = await import("./pkg-threads/duckosm_wasm.js");
      wasm = await pkg.default();
      await pkg.initThreadPool(data.threads);
    } else if (!pkg) {
      pkg = await import("./pkg/duckosm_wasm.js");
      // the module that quack_worker.js compiled one time for all the tile workers
      wasm = await pkg.default({ module_or_path: data.module });
    }
    if (!spill) {
      // the temporary files of this worker in OPFS (a folder of its own), opened one time for all its tiles
      const own = await data.dir.getDirectoryHandle(`spill-${crypto.randomUUID()}`, { create: true });
      spill = [];
      for (let k = 0; k < SPILL; k++) spill.push(await (await own.getFileHandle(`t${k}`, { create: true })).createSyncAccessHandle());
    }
    const file = await (await data.dir.getFileHandle(`tile-${data.i}.pbf`)).getFile();
    const out = await (await data.dir.getFileHandle(`part-${data.i}.parquet`, { create: true })).createSyncAccessHandle();
    const t = performance.now();
    try {
      pkg.quack_storage_ms();
      const r = pkg.quack_tile(file, data.tile[0], data.tile[1], data.tile[2], out, spill, data.sort);
      // the time that this tile waited for its file and for the browser storage
      const io = pkg.quack_storage_ms();
      out.flush();
      // the memory of this worker (each worker has its own WebAssembly memory; it never shrinks)
      const mib = Math.round(wasm.memory.buffer.byteLength / 2 ** 20);
      // begin: when this tile began, after the start of the worker (a time that all workers share)
      postMessage({ i: data.i, nodes: r.nodes, ways: r.ways, relations: r.relations, ms: performance.now() - t, io, mib, begin: performance.timeOrigin + t });
    } finally {
      out.close();
    }
  } catch (e) {
    postMessage({ i: data.i, error: String((e && e.message) || e) });
  }
};

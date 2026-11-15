// duckOSM's quack in a Web Worker (docs/rust/quack.md of duckOSM): the features table of QuackOSM (feature_id, tags,
// geometry) of a .osm.pbf file, made by the Rust engine in WebAssembly.
// In: {pbf: File, sort: true or false (default true: the order of QuackOSM), tiles: the most nodes in a tile, to make
// the table tile by tile (a large file), or null; zoom: tile by tile with the map tiles of this zoom, or null; cutThreads:
// the threads of the cut (and of one run); workers: the tile workers; joinThreads: the threads of the join (a worker of its
// own, quack_join_worker.js)}. Out: {nodes, ways, relations, steps: for
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
        threads = Math.min(data.cutThreads || data.workers || navigator.hardwareConcurrency || 4, 64);
        await pkg.initThreadPool(threads);
      }
    }
    for (let i = 0; i < SPILL; i++) spill.push(await (await dir.getFileHandle(`t${i}`, { create: true })).createSyncAccessHandle());
    const fh = await dir.getFileHandle("quack.parquet", { create: true });
    if (data.tiles || data.zoom != null) {
      await byTiles(data, dir, spill, fh);
      return;
    }
    out = await fh.createSyncAccessHandle();
    const t = performance.now();
    const r = pkg.quack(data.pbf, data.sort !== false, spill, out, data.tiles ?? undefined, data.zoom ?? undefined);
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

// Quack by tiles with a worker for each tile: (1) the tiles of the file, (2) the cut, each tile into its own file
// tile-<i>.pbf, (3) a pool of tile workers (quack_tile_worker.js, one for each core), each takes the next tile (the largest
// first) and writes part-<i>.parquet, (4) the join of the parts into quack.parquet. Only steps 2 and 4 run here.
async function byTiles(data, dir, spill, fh) {
  const t0 = performance.now();
  const steps = [];
  const mib = () => Math.round(wasm.memory.buffer.byteLength / 2 ** 20);
  const step = (name) => {
    const s = { step: name, ms: performance.now() - t0, mib: mib() };
    steps.push(s);
    postMessage(s);
  };
  const tiles = pkg.quack_plan(data.pbf, data.tiles ?? undefined, data.zoom ?? undefined);
  step(`quack by tiles 1: ${tiles.length} tiles`);
  const outs = [];
  for (let i = 0; i < tiles.length; i++) {
    outs.push(await (await dir.getFileHandle(`tile-${i}.pbf`, { create: true })).createSyncAccessHandle());
  }
  let counts;
  try {
    counts = pkg.quack_cut(data.pbf, tiles, outs, spill);
  } finally {
    for (const h of outs) h.close();
  }
  step("quack by tiles 2: the tile cut");
  // the largest tiles first, so the small ones fill the end and no worker waits for one large tile
  const todo = [];
  counts.forEach((n, i) => { if (n > 0) todo.push(i); });
  todo.sort((a, b) => counts[b] - counts[a]);
  const workers = Math.max(1, Math.min(data.workers || navigator.hardwareConcurrency || 4, 64, todo.length));
  const totals = { nodes: 0, ways: 0, relations: 0 };
  await new Promise((resolve, reject) => {
    const pool = [];
    let next = 0, done = 0;
    const stop = () => pool.forEach((w) => w.terminate());
    const give = (w) => {
      if (next < todo.length) {
        const i = todo[next++];
        w.postMessage({ dir, i, tile: tiles[i] });
      }
    };
    for (let k = 0; k < workers; k++) {
      const w = new Worker(new URL("./quack_tile_worker.js", import.meta.url), { type: "module" });
      w.onmessage = ({ data: r }) => {
        if (r.error) {
          stop();
          reject(new Error(`tile ${tiles[r.i].join("/")}: ${r.error}`));
          return;
        }
        totals.nodes += r.nodes;
        totals.ways += r.ways;
        totals.relations += r.relations;
        done++;
        step(`quack by tiles 3: tile ${done} of ${todo.length} (${tiles[r.i].join("/")}, ${(r.ms / 1000).toFixed(1)} s)`);
        if (done === todo.length) {
          stop();
          resolve();
        } else {
          give(w);
        }
      };
      w.onerror = (e) => {
        e.preventDefault();
        stop();
        reject(new Error(`a tile worker stopped (${e.message || "error"}): most likely it had no memory left`));
      };
      pool.push(w);
      give(w);
    }
    if (todo.length === 0) resolve();
  });
  for (let i = 0; i < tiles.length; i++) await dir.removeEntry(`tile-${i}.pbf`).catch(() => {});
  // the join in a worker of its own, with its own pool of threads (the setting "join")
  const joinThreads = Math.max(1, Math.min(data.joinThreads || threads, 64));
  await new Promise((resolve, reject) => {
    const w = new Worker(new URL("./quack_join_worker.js", import.meta.url), { type: "module" });
    w.onmessage = ({ data: r }) => {
      w.terminate();
      if (r.error) reject(new Error(`the join: ${r.error}`));
      else resolve();
    };
    w.onerror = (e) => {
      e.preventDefault();
      w.terminate();
      reject(new Error(`the join worker stopped (${e.message || "error"})`));
    };
    w.postMessage({ dir, parts: todo.map((i) => `part-${i}.parquet`), sort: data.sort !== false, threads: joinThreads });
  });
  // with the sort, the join is the sort
  step(data.sort !== false ? "quack 5: the sort" : "quack by tiles 4: the join of the parts");
  for (const i of todo) await dir.removeEntry(`part-${i}.parquet`).catch(() => {});
  const seconds = (performance.now() - t0) / 1000;
  postMessage({ ...totals, steps, file: await fh.getFile(), seconds, threads: `cut ${threads}, ${workers} tile workers, join ${joinThreads}`, mib: mib() });
}

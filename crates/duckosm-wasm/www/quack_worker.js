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
    postMessage({ nodes: r.nodes, ways: r.ways, relations: r.relations, steps: r.steps, file: await fh.getFile(), seconds, storageMib: Math.round(storageMax() / 2 ** 20), threads, mib });
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
  // the module of the tile workers, compiled one time while the plan and the cut run (the browser compiles it on threads
  // of its own); each tile worker then makes its instance from it at once (each worker compiled the 18 MB module again)
  const tileModule = WebAssembly.compileStreaming(fetch(new URL("./pkg/duckosm_wasm_bg.wasm", import.meta.url)));
  tileModule.catch(() => {});
  const steps = [];
  const mib = () => Math.round(wasm.memory.buffer.byteLength / 2 ** 20);
  // the memory of each tile worker (its own WebAssembly memory), added to the memory of this module
  const workerMib = new Map();
  // the most browser storage that the run used (each 0.5 s, while this worker waits; the cut is one call, so after it)
  let storage = 0;
  const poll = () => navigator.storage.estimate().then((e) => { storage = Math.max(storage, e.usage); });
  const timer = setInterval(poll, 500);
  try {
    return await byTilesRun(data, dir, spill, fh, t0, steps, mib, workerMib, () => storage, poll, tileModule);
  } finally {
    clearInterval(timer);
  }
}

async function byTilesRun(data, dir, spill, fh, t0, steps, mib, workerMib, storageMax, poll, tileModule) {
  const step = (name) => {
    const s = {
      step: name,
      ms: performance.now() - t0,
      mib: mib(),
      workersMib: [...workerMib.values()].reduce((a, b) => a + b, 0),
      // the memory of each tile worker, in the order of the workers
      workerMibs: [...workerMib.entries()].sort((a, b) => a[0] - b[0]).map((e) => e[1]),
      storageMib: Math.round(storageMax() / 2 ** 20),
    };
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
  await poll();
  step(`quack by tiles 2: the tile cut (${(pkg.quack_storage_ms() / 1000).toFixed(1)} s of it waiting for the file or the browser storage)`);
  // the largest tiles first, so the small ones fill the end and no worker waits for one large tile
  const todo = [];
  counts.forEach((n, i) => { if (n > 0) todo.push(i); });
  todo.sort((a, b) => counts[b] - counts[a]);
  // the threads of the tile workers (the setting "tile workers"). A tile with more than its share of the nodes gets a
  // worker with a pool of threads, its share of the threads: one large tile on one thread was the end of step 3 (Sweden
  // at zoom 6: 6/34/19 alone for 24 s). The other tiles go to workers with one thread each.
  const budget = Math.max(1, Math.min(data.workers || navigator.hardwareConcurrency || 4, 64));
  const sum = todo.reduce((a, i) => a + counts[i], 0) || 1;
  const big = [];
  let left = budget;
  for (const i of todo) {
    const t = Math.min(Math.round((budget * counts[i]) / sum), left - 1);
    if (t < 2 || !self.crossOriginIsolated) break;
    big.push([i, t]);
    left -= t;
  }
  const queue = todo.slice(big.length);
  const workers = big.length + Math.max(big.length ? 0 : 1, Math.min(left, queue.length));
  const threadsOf = (k) => (k < big.length ? big[k][1] : 1);
  const totals = { nodes: 0, ways: 0, relations: 0 };
  // the timeline of step 3: for each tile, its worker, the time it went to the worker and the time it came back (ms)
  const timeline = [];
  const started = new Map();
  const module = await tileModule;
  await new Promise((resolve, reject) => {
    const pool = [];
    let next = 0, done = 0;
    const stop = () => pool.forEach((w) => w.terminate());
    // the tile of each worker now (null: no tile), for the page while the workers run
    const working = new Array(workers).fill(null);
    const live = () => postMessage({
      live: true, mib: mib(), done, total: todo.length,
      workersMib: [...workerMib.values()].reduce((a, b) => a + b, 0),
      workerMibs: working.map((_, k) => workerMib.get(k) || 0),
      workerTiles: working.map((i, k) => (i === null ? null : tiles[i].join("/") + (threadsOf(k) > 1 ? ` on ${threadsOf(k)} threads` : ""))),
    });
    // a worker of a large tile takes that tile first; then each worker takes the next tile of the queue
    const give = (w, k) => {
      working[k] = null;
      const i = k < big.length && !started.has(big[k][0]) ? big[k][0] : next < queue.length ? queue[next++] : null;
      if (i !== null) {
        started.set(i, [k, performance.now() - t0]);
        working[k] = i;
        w.postMessage({ dir, i, tile: tiles[i], module, threads: threadsOf(k) });
      }
    };
    for (let k = 0; k < workers; k++) workerMib.set(k, 0);
    for (let k = 0; k < workers; k++) {
      const w = new Worker(new URL("./quack_tile_worker.js", import.meta.url), { type: "module" });
      w.onmessage = ({ data: r }) => {
        // a step in the tile (each pass of quack): the memory of the worker now
        if (r.step !== undefined) {
          workerMib.set(k, r.mib);
          live();
          return;
        }
        if (!r.error) {
          workerMib.set(k, r.mib);
          const [worker, given] = started.get(r.i);
          // the start of the tile in the worker (after the start of a new worker); `given`: when the tile went to it
          const start = r.begin - performance.timeOrigin - t0;
          timeline.push({ worker, start, given, end: performance.now() - t0, tile: tiles[r.i].join("/"), nodes: counts[r.i] });
        }
        if (r.error) {
          stop();
          // a full storage: say how full, from the browser's own numbers
          navigator.storage.estimate().then(({ usage, quota }) => {
            const full = /Quota|space/i.test(r.error) ? ` (browser storage ${(usage / 2 ** 20).toFixed(0)} of ${(quota / 2 ** 20).toFixed(0)} MB, at most ${(storageMax() / 2 ** 20).toFixed(0)} MB in this run)` : "";
            reject(new Error(`tile ${tiles[r.i].join("/")}${full}: ${r.error}`));
          });
          return;
        }
        // the tile file is not necessary after its rows are made: free its storage at once
        dir.removeEntry(`tile-${r.i}.pbf`).catch(() => {});
        totals.nodes += r.nodes;
        totals.ways += r.ways;
        totals.relations += r.relations;
        done++;
        step(`quack by tiles 3: tile ${done} of ${todo.length} (${tiles[r.i].join("/")}, ${(r.ms / 1000).toFixed(1)} s)`);
        if (done === todo.length) {
          stop();
          resolve();
        } else {
          give(w, k);
          live();
        }
      };
      w.onerror = (e) => {
        e.preventDefault();
        stop();
        reject(new Error(`a tile worker stopped (${e.message || "error"}): most likely it had no memory left`));
      };
      pool.push(w);
      give(w, k);
    }
    live();
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
  const workersMib = [...workerMib.values()].reduce((a, b) => a + b, 0);
  postMessage({ ...totals, steps, timeline, workers, workersMib, file: await fh.getFile(), seconds, storageMib: Math.round(storageMax() / 2 ** 20), threads: `cut ${threads}, ${workers} tile workers` + (big.length ? ` (the largest ${big.length === 1 ? "tile" : `${big.length} tiles`} on ${big.map((b) => b[1]).join(", ")} threads)` : "") + `, join ${joinThreads}`, mib: mib() });
}

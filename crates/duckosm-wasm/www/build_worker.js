// The build of duckOSM Studio in a Web Worker, so the page stays responsive during a long build.
// In: {pbf: ArrayBuffer} (a file cut to the area; with a boundary, the build by tiles too), {region: File, boundary: GeoJSON text, raw:
// true or false} (a region file: the build by tiles, byTiles; raw: also duckOSM's raw.* files, docs/features.md) or {json:
// ArrayBuffer, boundary, raw} (OSM JSON from the Overpass API; raw: also raw.*, from the objects of the layers), with {dem: ArrayBuffer and demName: a GeoTIFF DEM, or none; demProduct, demDatum, demLicense: what the person knows of it, demNotReference: true for a DEM that is not the reference; modes: "driving,walking", fixes: text or null}.
// In, for the build by tiles: tileZoom, tileWorkers (byTiles). {cut: true, region, boundary, folder}: only step 0 (cutOnly), {file}.
// Out: {step}, then {files: {name: Uint8Array, or File in OPFS for a region file}, summary, seconds: {load, build},
// memoryMiB, threads, featuresSql, log, folder} (the buffers are transferred, not copied; a region file: each file of the
// result is a File in OPFS, in `folder` inside data.folder (the folder of the page's tab), and the page removes it; featuresSql: the SQL of the base-map layers, with raw; log: [{step, ms,
// mib}], the time of each step and the WebAssembly memory after it), or {error}. After {error}, the page makes a new
// worker.
// A cross-origin isolated page (the COOP and COEP headers, www/serve.py) gets the module with threads
// (pkg-threads, rayon on Web Workers); another page gets the module with one thread (pkg).

let wasm = null, pkg = null, threads = 1;

onmessage = async ({ data }) => {
  try {
    const t0 = performance.now();
    if (!wasm) {
      postMessage({ step: "loading the WebAssembly module" });
      const isolated = self.crossOriginIsolated;
      pkg = await import(isolated ? "./pkg-threads/duckosm_wasm.js" : "./pkg/duckosm_wasm.js");
      wasm = await pkg.default();
      if (isolated) {
        // up to 12 threads: Kentucky from the US South file with the layers and the faces took 46.7 s with 12 threads
        // and 50.4 s with 8, for 45 MiB more memory (Mac mini M6, 12 cores)
        threads = data.threads || Math.min(navigator.hardwareConcurrency || 4, 12);
        // a thread worker that cannot load its module (a network fault) never reports, and initThreadPool then waits for ever
        await Promise.race([pkg.initThreadPool(threads), new Promise((_, no) => setTimeout(() =>
          no(new Error(`the ${threads} threads of the build did not start in 60 s: a file of the page did not load (the network)`)), 60_000))]);
      }
    }
    if (data.cut) {
      postMessage({ file: await cutOnly(data) });
      return;
    }
    // a .osm.pbf file with a boundary builds by tiles as a region file does; without a boundary there is no area to cut to tiles
    if (data.pbf && data.boundary) data.region = new Blob([data.pbf]);
    const { build, build_osm_json, features_sql } = pkg;
    const t1 = performance.now();
    const mib = () => Math.round(wasm.memory.buffer.byteLength / 2 ** 20 * 10) / 10, loadMib = mib();
    postMessage({ step: "building the networks" });
    const fixes = data.fixes || undefined;
    const levels = data.levels || undefined;   // "tags" (the default) or "solve"
    pkg.set_dem(data.demName, data.dem ? new Uint8Array(data.dem) : undefined, data.demProduct, data.demDatum, data.demLicense, !!data.demNotReference);   // the DEM (a GeoTIFF) of the area, or none
    pkg.set_timezone_entry(data.tz);           // the entry of the page's time zone table, or undefined: no time zone
    pkg.set_building_faces(data.walls !== false);   // "Wall labels": main.building_faces with the layers (on when the page says nothing)
    const pre = [];   // the steps of the build by tiles (byTiles)
    const res = data.region
      ? await buildArea(data, fixes, levels, pre, mib)
      : data.json
        ? build_osm_json(new Uint8Array(data.json), data.boundary || undefined, data.modes, fixes, levels, !!data.raw)
        : build(new Uint8Array(data.pbf), data.modes, fixes, data.boundary || undefined, levels);
    const t2 = performance.now();
    const log = [{ step: "load the WebAssembly module", ms: t1 - t0, mib: loadMib }, ...pre, ...JSON.parse(res.log)];
    // one buffer for each file: duckdb-wasm registers a file as one buffer (a region file: Files in OPFS, no join)
    const files = res.opfs ? res.files : {};
    for (const name of res.opfs ? [] : Object.keys(res.files)) {
      const parts = res.files[name];
      const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
      let at = 0;
      for (const p of parts) {
        out.set(p, at);
        at += p.length;
      }
      files[name] = out;
    }
    log.push({ step: "join the parts of the files", ms: performance.now() - t2, mib: mib() });
    postMessage(
      {
        files,
        summary: res.summary,
        seconds: { load: (t1 - t0) / 1000, build: (t2 - t1) / 1000 },
        // WebAssembly memory only grows, so its size is the peak of all builds of this worker; by tiles, with the most memory of the
        // workers that ran at the same time (the tiles, or the union)
        memoryMiB: Math.round(wasm.memory.buffer.byteLength / 2 ** 20 + (res.tiles ? Math.max(res.tiles.workersMib, res.tiles.unionMib) : 0)),
        tiles: res.tiles,
        threads,
        featuresSql: data.raw && (data.region || data.json) ? features_sql() : undefined,
        log,
        folder: res.folder,
      },
      Object.values(files).flatMap((f) => (f.buffer ? [f.buffer] : [])),
    );
  } catch (e) {
    // after a panic, the module's panic hook has kept the message (e is only "RuntimeError: unreachable")
    postMessage({ error: self.duckosmPanic || String((e && e.message) || e) });
  }
};

// A region file (or a .osm.pbf file with a boundary): the build by tiles (byTiles), for every area. Its files are in OPFS, in a folder of
// its own (two pages can build at the same time), in data.folder (the folder of the page's tab) when the page gives one. After the build,
// only the files of the result stay in it: the page removes the folder with its next build, or the page removes the folders of closed tabs.
async function buildArea(data, fixes, levels, pre, mib) {
  const root = await navigator.storage.getDirectory();
  const parent = data.folder ? await root.getDirectoryHandle(data.folder, { create: true }) : root;
  const name = `build-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const dir = await parent.getDirectoryHandle(name, { create: true });
  let res = null;
  try {
    res = await byTiles(data, dir, data.region, fixes, levels, pre, mib);
  } finally {
    if (!res) await parent.removeEntry(name, { recursive: true }).catch(() => {});
  }
  return { ...res, opfs: true, folder: data.folder ? name : undefined };
}

// The cut of the region file to the area (step 0 of duckOSM, the file of the page's .osm.pbf button): {cut: true, region, boundary,
// folder} gives {file: a File in OPFS, folder}. The build does not need it, so the page asks for it only when the person wants the file.
async function cutOnly(data) {
  const root = await navigator.storage.getDirectory();
  const parent = data.folder ? await root.getDirectoryHandle(data.folder, { create: true }) : root;
  const fh = await parent.getFileHandle(`cut-${Date.now()}.osm.pbf`, { create: true });
  const h = await fh.createSyncAccessHandle();
  try {
    pkg.cut_area(data.region, data.boundary, h);
  } finally {
    h.close();
  }
  return fh.getFile();
}

// The build by tiles (src/tiles.rs; duckOSM docs/design/build_by_tiles.md), the steps of `duckosm-rust tiles`: (1) the cut of the
// area file to the map tiles of zoom 8 (?tilezoom=N), here; (2) the sidewalk step of each tile, when the build has walking; (3) the
// build of each tile, each in a worker of its own (build_tile_worker.js, one thread, ?tileworkers=N at a time, the largest tile
// first); (4) the union and the steps after it in one more worker. Each file of a step is a file of OPFS in `dir`, and a step
// deletes the files that it has read (the tile files after their tile, the tables of the tiles during the union). The files of
// the result stay in `dir`.
// `area`: the region file (or the .osm.pbf file): the tile cut reads it and cuts it to the boundary.
async function byTiles(data, dir, area, fixes, levels, pre, mib) {
  const t0 = performance.now();
  let t = t0;
  // the WebAssembly memory of the steps: this module, and the sum of the tile workers that run at the same time
  let peakWorkers = 0, unionMib = 0;
  const done = (step) => {
    pre.push({ step, ms: performance.now() - t, mib: mib() });
    t = performance.now();
  };
  // the most browser storage of the build (each 0.5 s)
  let storage = 0;
  const poll = () => navigator.storage.estimate().then((e) => { storage = Math.max(storage, e.usage); });
  const timer = setInterval(poll, 500);
  try {
    // the module of the workers, compiled one time while the cut runs (the browser compiles on threads of its own); each worker
    // makes its instance from it at once
    const compiling = WebAssembly.compileStreaming(fetch(new URL("./pkg/duckosm_wasm_bg.wasm", import.meta.url)));
    compiling.catch(() => {});
    const boundary = new TextEncoder().encode(data.boundary);
    const common = { "boundary.geojson": boundary };
    if (fixes) common["osm_fixes.yaml"] = new TextEncoder().encode(fixes);
    const opts = {
      dir, modes: data.modes, raw: !!data.raw, walls: data.walls, tz: data.tz, levels,
      dem: data.dem, demName: data.demName, demProduct: data.demProduct, demDatum: data.demDatum, demLicense: data.demLicense, demNotReference: data.demNotReference,
    };
    // where each file of a step is: name -> its file in OPFS, and name -> File
    const where = new Map(), have = new Map();
    const keep = (files) => {
      for (const [n, f] of Object.entries(files)) {
        have.set(n, f);
        where.set(n, f.name);
      }
    };
    const drop = (n) => {
      if (where.has(n)) dir.removeEntry(where.get(n)).catch(() => {});
      where.delete(n);
      have.delete(n);
    };

    // 1. the cut
    postMessage({ step: "cutting the area to tiles" });
    const zoom = data.tileZoom ?? 8;
    const list = pkg.tile_list(data.boundary, zoom);
    const cutSlots = 2 * list.length, slots = [];
    let temp = null;
    const open = async (n) => (await dir.getFileHandle(n, { create: true })).createSyncAccessHandle();
    let cut;
    try {
      slots.push(...await Promise.all(Array.from({ length: cutSlots }, (_, i) => open(`c-s${i}`))));
      temp = await open("c-t");
      cut = pkg.tiles_cut({ "area.osm.pbf": area, ...common }, zoom, data.modes, !!data.raw, slots, temp);
    } finally {
      for (const h of [...slots, temp].filter(Boolean)) h.close();
    }
    const used = new Set(Object.values(cut.files).map((i) => `c-s${i}`));
    await Promise.all(Object.entries(cut.files).map(async ([n, i]) => keep({ [n]: await (await dir.getFileHandle(`c-s${i}`)).getFile() })));
    for (let i = 0; i < cutSlots; i++) if (!used.has(`c-s${i}`)) dir.removeEntry(`c-s${i}`).catch(() => {});
    dir.removeEntry("c-t").catch(() => {});
    opts.module = await compiling;
    const tiles = cut.tiles;
    const tname = (tl) => tl.join("_");
    done(`cut to ${tiles.length} tiles of zoom ${zoom}`);

    // a worker for each step; `onRemove`: the union has read a file
    let k = 0, unionStep = "start";
    const run = (msg) => new Promise((resolve, reject) => {
      const w = new Worker(new URL("./build_tile_worker.js", import.meta.url), { type: "module" });
      w.onmessage = ({ data: r }) => {
        if (r.remove !== undefined) return drop(r.remove);
        if (r.step !== undefined) return msg.onStep?.(r);
        w.terminate();
        for (const n of r.unused ?? []) dir.removeEntry(n).catch(() => {});   // not waited for: the next step does not need it
        if (r.error) reject(new Error(`${msg.what}${msg.kind === "union" ? ` (after the step "${unionStep}")` : ""}: ${r.error}`));
        else resolve(r);
      };
      w.onerror = (e) => {
        e.preventDefault();
        w.terminate();
        reject(new Error(`${msg.what}: the worker stopped (${e.message || "error"}): most likely it had no memory left`));
      };
      const { onStep, what, ...m } = msg;
      w.postMessage({ ...opts, ...m, prefix: `w${k++}` });
    });
    // the tiles, the largest file first, at most `workers` at a time
    const size = (tl) => have.get(`tiles/${tname(tl)}.osm.pbf`).size;
    const order = [...tiles].sort((a, b) => size(b) - size(a));
    const workers = Math.max(1, Math.min(data.tileWorkers || navigator.hardwareConcurrency || 4, 64, order.length));
    const pool = async (each) => {
      const running = new Map();
      let next = 0, n = 0;
      await Promise.all(Array.from({ length: workers }, async () => {
        while (next < order.length) {
          const tl = order[next++];
          running.set(tl, 0);
          const r = await each(tl, (mib) => {
            running.set(tl, mib);
            peakWorkers = Math.max(peakWorkers, [...running.values()].reduce((a, b) => a + b, 0));
          });
          running.delete(tl);
          postMessage({ step: `building the tiles: ${++n} of ${order.length}` });
          if (r) keep(r);
        }
      }));
    };
    const tileGiven = (tl) => ({ ...common, [`tiles/${tname(tl)}.osm.pbf`]: have.get(`tiles/${tname(tl)}.osm.pbf`), [`tiles/${tname(tl)}.junctions`]: have.get(`tiles/${tname(tl)}.junctions`) });

    // 2. the sidewalk step: a road crosses tiles, so each tile decides its own roads with the lines of the tiles near it
    const walking = data.modes.split(",").includes("walking");
    // the files of the result of a tile: each table of each mode, and its border rows apart (duckOSM union::TABLES: 21 tables)
    const nModes = data.modes.split(",").length, tileSlots = 48 * nModes + 16;
    if (walking) {
      const ids = [];
      const allTiles = Object.fromEntries([...have].filter(([n]) => n.endsWith(".osm.pbf")));
      await pool(async (tl, mem) => {
        const r = await run({ what: `the sidewalk step of tile ${tl.join("/")}`, kind: "sidewalks", tile: tl, slots: 4, given: { ...tileGiven(tl), ...allTiles }, onStep: (s) => mem(s.mib) });
        mem(r.mib);
        for (const f of Object.values(r.files)) {
          ids.push(new BigInt64Array(await f.arrayBuffer()));
          await dir.removeEntry(f.name).catch(() => {});
        }
      });
      const all = new BigInt64Array(ids.reduce((n, a) => n + a.length, 0));
      let at = 0;
      for (const a of ids) {
        all.set(a, at);
        at += a.length;
      }
      all.sort();
      common["sidewalks.all"] = new Uint8Array(all.buffer);
      done(`the sidewalk step: ${all.length} roads get sidewalk=separate`);
    }

    // 3. the tile builds
    await pool(async (tl, mem) => {
      const r = await run({ what: `tile ${tl.join("/")}`, kind: "tile", tile: tl, slots: tileSlots, given: tileGiven(tl), onStep: (s) => mem(s.mib) });
      mem(r.mib);
      // the tile file and its junctions are read: they go at once
      drop(`tiles/${tname(tl)}.osm.pbf`);
      drop(`tiles/${tname(tl)}.junctions`);
      return r.files;
    });
    done(`${tiles.length} tile builds (${workers} at a time)`);

    // 4. the union and the steps after it
    postMessage({ step: "the union of the tiles" });
    const given = { ...common, ...Object.fromEntries([...have].filter(([n]) => n.startsWith("tiles/"))) };
    // the files of the result of the union: its tables (its temporary files are blocks of one file, any number of them)
    const u = await run({ what: "the union", kind: "union", tiles, slots: 32 * nModes + 16, given, onStep: (s) => {
      unionStep = s.step;
      unionMib = Math.max(unionMib, s.mib);
      postMessage({ step: `the union: ${s.step}` });
    } });
    unionMib = Math.max(unionMib, u.mib);
    done("the union, the bus routes, the levels and the area tables");
    // the files of the tiles that the union did not read (a table that no mode has)
    for (const n of [...have.keys()].filter((n) => n.startsWith("tiles/"))) drop(n);
    const files = {};
    for (const [n, f] of Object.entries(u.files)) {
      if (n.startsWith("out/")) files[n.slice(4)] = f;
      else await dir.removeEntry(f.name).catch(() => {});
    }
    // a Parquet file has at least its two marks and the length of its footer (12 bytes)
    const bad = Object.entries(files).filter(([, f]) => f.size < 12);
    if (bad.length) throw new Error(`the union gave files with no Parquet in them: ${bad.map(([n, f]) => `${n} (${f.size} bytes, ${f.name}, ${u.rows?.[n.replace(/\.parquet$/, "")]} rows)`).join(", ")}`);
    await poll();
    return {
      byTiles: true, files, summary: u.summary, log: "[]",
      tiles: { count: tiles.length, zoom, workers, workersMib: peakWorkers, unionMib, storageMib: Math.round(storage / 2 ** 20), seconds: (performance.now() - t0) / 1000 },
    };
  } finally {
    clearInterval(timer);
  }
}

// The build of duckOSM Studio in a Web Worker, so the page stays responsive during a long build.
// In: {pbf: ArrayBuffer} (a file cut to the area), {region: File, boundary: GeoJSON text, raw: true or false} (a
// region file: step 0 cuts it to an area file in OPFS, read piece by piece; raw: also duckOSM's raw.* files, docs/features.md) or {json:
// ArrayBuffer, boundary, raw} (OSM JSON from the Overpass API; raw: also raw.*, from the objects of the layers), with {dem: ArrayBuffer and demName: a GeoTIFF DEM, or none; demProduct, demDatum, demLicense: what the person knows of it, demNotReference: true for a DEM that is not the reference; modes: "driving,walking", fixes: text or null}.
// Out: {step}, then {files: {name: Uint8Array}, summary, seconds: {load, build}, memoryMiB, threads, featuresSql, log}
// (the buffers are transferred, not copied; featuresSql: the SQL of the base-map layers, with raw; log: [{step, ms,
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
        // more than 8 threads gave no more speed on Tartu from the Estonia file, only more memory
        threads = data.threads || Math.min(navigator.hardwareConcurrency || 4, 8);
        await pkg.initThreadPool(threads);
      }
    }
    const { build, build_osm_json, features_sql } = pkg;
    const t1 = performance.now();
    const mib = () => Math.round(wasm.memory.buffer.byteLength / 2 ** 20 * 10) / 10, loadMib = mib();
    postMessage({ step: "building the networks" });
    const fixes = data.fixes || undefined;
    const levels = data.levels || undefined;   // "tags" (the default) or "solve"
    pkg.set_dem(data.demName, data.dem ? new Uint8Array(data.dem) : undefined, data.demProduct, data.demDatum, data.demLicense, !!data.demNotReference);   // the DEM (a GeoTIFF) of the area, or none
    pkg.set_timezone_entry(data.tz);           // the entry of the page's time zone table, or undefined: no time zone
    pkg.set_building_faces(data.walls !== false);   // "Wall labels": main.building_faces with the layers (on when the page says nothing)
    const pre = [];   // the steps before the build: step 0
    const res = data.region
      ? await buildArea(data, fixes, levels, pre, mib)
      : data.json
        ? build_osm_json(new Uint8Array(data.json), data.boundary || undefined, data.modes, fixes, levels, !!data.raw)
        : build(new Uint8Array(data.pbf), data.modes, fixes, data.boundary || undefined, levels);
    const t2 = performance.now();
    const log = [{ step: "load the WebAssembly module", ms: t1 - t0, mib: loadMib }, ...pre, ...JSON.parse(res.log)];
    // one buffer for each file: duckdb-wasm registers a file as one buffer
    const files = {};
    for (const name of Object.keys(res.files)) {
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
        // WebAssembly memory only grows, so its size is the peak of all builds of this worker
        memoryMiB: Math.round(wasm.memory.buffer.byteLength / 2 ** 20),
        threads,
        featuresSql: data.raw && (data.region || data.json) ? features_sql() : undefined,
        log,
      },
      Object.values(files).map((f) => f.buffer),
    );
  } catch (e) {
    // after a panic, the module's panic hook has kept the message (e is only "RuntimeError: unreachable")
    postMessage({ error: self.duckosmPanic || String((e && e.message) || e) });
  }
};

// A region file: step 0 cuts it to the area file in OPFS (only lists of ids in memory), then the build reads the area file. The
// temporary files of the network step go to a pool of OPFS files. A file of OPFS opens only asynchronously, so the worker opens them
// here, before the synchronous build, in a folder of its own (two pages can build at the same time), and removes the folder after.
async function buildArea(data, fixes, levels, pre, mib) {
  const root = await navigator.storage.getDirectory();
  const name = `build-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const dir = await root.getDirectoryHandle(name, { create: true });
  const spill = [];
  try {
    const t = performance.now();
    postMessage({ step: "cutting the region file to the area" });
    const fh = await dir.getFileHandle("area.osm.pbf", { create: true });
    const h = await fh.createSyncAccessHandle();
    try {
      pkg.cut_area(data.region, data.boundary, h);
    } finally {
      h.close();
    }
    pre.push({ step: "cut to the boundary", ms: performance.now() - t, mib: mib() });
    postMessage({ step: "building the networks" });
    // ponytail: 32 files; Kentucky needs at most 11 at once
    for (let i = 0; i < 32; i++) spill.push(await (await dir.getFileHandle(`t${i}`, { create: true })).createSyncAccessHandle());
    return pkg.build_area_file(await fh.getFile(), data.boundary, data.modes, fixes, !!data.raw, levels, spill);
  } finally {
    for (const h of spill) h.close();
    await root.removeEntry(name, { recursive: true }).catch(() => {});
  }
}

// One step of the build by tiles in a Web Worker of its own (build_worker.js starts one for each step; src/tiles.rs): the build
// of one tile, the sidewalk step of one tile, or the union. Each worker has its own WebAssembly module with one thread (the
// files of the page are on one thread) and its own memory: a new worker for each tile, so the memory of a large tile goes back
// when the tile ends.
// In: {kind: "tile", "sidewalks" or "union", dir: the folder of the build in OPFS, prefix: the start of the names of the OPFS
// files of this step, slots: the number of the files of the result that the step can write (its other files are blocks of one file), given: {name: File or Uint8Array}, tile: [z, x, y]
// or tiles: [[z, x, y], ...] (the union), modes, raw, levels, walls, tz, dem, demName, demProduct, demDatum, demLicense,
// demNotReference, module: the compiled module}. Out: {step, ms, mib} (each step of the engine), {remove: name} (the union has
// read a given file: it can go), then {files: {name: File}, summary, rows, mib} or {error}.

onmessage = async ({ data }) => {
  const slots = [];
  let temp = null;
  const name = (k, i) => `${data.prefix}-${k}${i}`;
  try {
    const pkg = await import("./pkg/duckosm_wasm.js");
    const wasm = await pkg.default({ module_or_path: data.module });
    pkg.set_building_faces(data.walls !== false);
    pkg.set_timezone_entry(data.tz);
    pkg.set_dem(data.demName, data.dem ? new Uint8Array(data.dem) : undefined, data.demProduct, data.demDatum, data.demLicense, !!data.demNotReference);
    const open = async (n) => (await data.dir.getFileHandle(n, { create: true })).createSyncAccessHandle();
    // ponytail: the files open one after the other (about 0.2 s for the 160 files of a tile); opened at the same time
    // (Promise.all), a build stopped for ever now and then (dem_e2e.py, 2026-11-17)
    for (let i = 0; i < data.slots; i++) slots.push(await open(name("s", i)));
    // the temporary files of the step, named or not, are blocks of one file (src/tiles.rs, duckosm_core::fs::Blocks)
    temp = await open(name("t", ""));
    const res = data.kind === "union"
      ? pkg.tiles_union(data.given, data.tiles, data.modes, !!data.raw, data.levels, slots, temp)
      : pkg.tile_build(data.given, data.tile, data.modes, !!data.raw, data.kind === "sidewalks", slots, temp);
    for (const h of [...slots, temp]) h.close();
    slots.length = 0;
    temp = null;
    const files = {}, used = new Set();
    for (const [file, i] of Object.entries(res.files)) {
      files[file] = await (await data.dir.getFileHandle(name("s", i))).getFile();
      used.add(name("s", i));
    }
    // the files of the pool that the step did not use, and the temporary file
    for (let i = 0; i < data.slots; i++) if (!used.has(name("s", i))) await data.dir.removeEntry(name("s", i)).catch(() => {});
    await data.dir.removeEntry(name("t", "")).catch(() => {});
    // the memory of this worker (its WebAssembly memory never shrinks: the most that the step used)
    postMessage({ files, summary: res.summary, rows: res.rows, mib: Math.round(wasm.memory.buffer.byteLength / 2 ** 20) });
  } catch (e) {
    for (const h of [...slots, temp].filter(Boolean)) h.close();
    postMessage({ error: self.duckosmPanic || String((e && e.message) || e) });
  }
};

// The join of quack by tiles in a Web Worker of its own, so it has its own pool of threads (the setting "join" of the
// page; the cut and the tile workers have their own settings). In: {dir: the folder of the run in OPFS, parts: the names
// of the parts of the tiles, sort: true or false (true: the parts are sorted, and the join merges them), threads, gdal:
// GDAL's layers, geofabrik: Geofabrik's 20 layers in place of GDAL's (with gdal), format: "geoparquet", "gpkg", "both"
// or (geofabrik) "shp"}. It writes quack.parquet (gdal: one GeoParquet file for each layer, <layer>.parquet, the
// GeoPackage quack.gpkg, directly in its OPFS file, or the zip file of shapefiles quack.shp.zip). Out: {merged, total}
// (the rows of the merge, while it runs), {done: i} (all rows of part i are read: its file can go), then {rows, ms, mib:
// the memory of this worker} (gdal: also {layers: the rows of each layer}) or {error}.

// the temporary files of the sort (a strip of QuackOSM's order that is larger than the budget; gdal: the members of the
// relations and the sort of them)
const SPILL = 8;
const SPILL_GDAL = 64;
// GDAL's layers, in their order
const LAYERS = ["points", "lines", "multilinestrings", "multipolygons", "other_relations"];
// Geofabrik's layers, in their order (duckOSM geofabrik.rs)
const GEOFABRIK = ["traffic", "places", "natural", "pois", "transport", "pofw", "roads", "railways", "waterways", "pois_a",
  "buildings_a", "landuse_a", "traffic_a", "pofw_a", "water_a", "natural_a", "transport_a", "adminareas_a", "places_a",
  "protected_areas_a"].map((l) => `gis_osm_${l}_free`);

onmessage = async ({ data }) => {
  const spill = [];
  let out = null, shp = null;
  const outs = [];
  try {
    const isolated = self.crossOriginIsolated;
    const pkg = await import(isolated ? "./pkg-threads/duckosm_wasm.js" : "./pkg/duckosm_wasm.js");
    const wasm = await pkg.default();
    if (isolated) await pkg.initThreadPool(Math.max(1, data.threads));
    const files = [];
    for (const name of data.parts) files.push(await (await data.dir.getFileHandle(name)).getFile());
    if (data.sort || data.gdal) {
      // GDAL's layers: the members of the relations wait in a file, and their sort needs a file for each run (France: 8
      // were not enough)
      const n = data.gdal ? SPILL_GDAL : SPILL;
      for (let i = 0; i < n; i++) spill.push(await (await data.dir.getFileHandle(`j${i}`, { create: true })).createSyncAccessHandle());
    }
    if (data.gdal) {
      const format = data.format || "geoparquet";
      const names = data.geofabrik ? GEOFABRIK : LAYERS;
      if (format === "geoparquet" || format === "both") for (const l of names) outs.push(await (await data.dir.getFileHandle(`${l}.parquet`, { create: true })).createSyncAccessHandle());
      const t = performance.now();
      if (format === "gpkg" || format === "both") out = await (await data.dir.getFileHandle("quack.gpkg", { create: true })).createSyncAccessHandle();
      if (format === "shp") shp = await (await data.dir.getFileHandle("quack.shp.zip", { create: true })).createSyncAccessHandle();
      const layers = pkg.quack_gdal_join(files, spill, outs, out ?? undefined, shp ?? undefined, !!data.geofabrik);
      for (const h of outs) h.flush();
      if (out) out.flush();
      if (shp) shp.flush();
      const rows = names.reduce((a, l) => a + layers[l], 0);
      postMessage({ rows, layers, ms: performance.now() - t, mib: Math.round(wasm.memory.buffer.byteLength / 2 ** 20) });
      return;
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
    if (shp) shp.close();
    for (const h of outs) h.close();
    for (const h of spill) h.close();
    for (let i = 0; i < spill.length; i++) await data.dir.removeEntry(`j${i}`).catch(() => {});
  }
};

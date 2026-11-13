// duckOSM Studio's files on the browser disk (OPFS), for the row "Studio keeps ... in this browser" and its Delete all. All pages of one
// site share one OPFS (all of khoshkhah.github.io, also the other pages there), so this module counts and deletes only Studio's own
// entries, by name, never the whole OPFS:
//   regions/            the kept region files (app/regions.js)
//   areas/              the saved areas (app/area_store.js)
//   tables-<id>/        the tables of a tab (app/duck.js); the folder of this tab and of each other open tab (it holds a Web Lock of the
//                       same name) stays
//   build-*/            the files of a build that a page did not remove (build_worker.js without the folder of a tab)
//   ground.pmtiles      the terrain of the open area (index.html): it stays
//   lv-lv-part_<name>/  the edits of roadstyle's level editor for an area of Studio (only the names of Studio's areas: another page of the
//                       site can use the same editor); the open area's stay
const levels = (name) => "lv-" + ("lv-part:" + name).replace(/[^\w.-]/g, "_");     // roadstyle's browserStore("lv-part:" + name)

async function bytesOf(handle) {
  if (handle.kind === "file") return (await handle.getFile()).size;
  let n = 0;
  for await (const [, h] of handle.entries()) n += await bytesOf(h);
  return n;
}

// Studio's entries at the root of OPFS: [{name, handle, bytes, stays}]. keep: the folder of this tab; areaNames: the names of the saved areas;
// openArea: the name of the open area, or null.
async function entries({ keep, areaNames = [], openArea = null }) {
  const root = await navigator.storage.getDirectory();
  const held = new Set(navigator.locks ? (await navigator.locks.query()).held.map((l) => l.name) : []);
  const lv = new Set([...areaNames, openArea].filter(Boolean).map(levels));
  const out = [];
  for await (const [name, handle] of root.entries()) {
    const tables = name.startsWith("tables-");
    if (!(name === "regions" || name === "areas" || tables || name.startsWith("build-") || name === "ground.pmtiles" || lv.has(name))) continue;
    const stays = name === keep || (tables && held.has(name)) || name === "ground.pmtiles" || (openArea && name === levels(openArea));
    out.push({ name, handle, bytes: await bytesOf(handle).catch(() => 0), stays });
  }
  return out;
}

/** The size of Studio's files in this browser: {bytes, staying} (staying: the bytes that Delete all keeps). */
export async function use(opts) {
  const list = await entries(opts);
  return { bytes: list.reduce((n, e) => n + e.bytes, 0), staying: list.filter((e) => e.stays).reduce((n, e) => n + e.bytes, 0) };
}

/** Delete Studio's files that do not stay; gives the bytes deleted. */
export async function clear(opts) {
  const root = await navigator.storage.getDirectory();
  let n = 0;
  for (const e of await entries(opts)) {
    if (e.stays) continue;
    await root.removeEntry(e.name, { recursive: true }).then(() => (n += e.bytes), () => {});
  }
  return n;
}

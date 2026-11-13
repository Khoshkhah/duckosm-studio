// The region files kept in the browser (docs/relay.md): a Geofabrik extract comes through the relay one time and stays in the storage
// of the site (OPFS, the Origin Private File System), so the next area in the same region needs no download. The build reads the kept
// file as it reads a file that the user selects.

// The address of the relay (relay/worker.js) after its deploy; empty: no relay, the user downloads the file. ?relay=<address> in the
// address of the page gives another one (?relay=relay: the relay of serve.py).
const RELAY = "https://duckosm-relay.khoshkhah.workers.dev";
export const relay = new URLSearchParams(location.search).get("relay") ?? RELAY;

const folder = () => navigator.storage.getDirectory().then((d) => d.getDirectoryHandle("regions", { create: true }));
const key = (id) => id.replace(/[^a-z0-9_-]/g, "_");     // a Geofabrik id can have a "/" (us/california)

// the kept files: [{id, name, pbf, size, modified, file}]; a file without its .json (a download that stopped) does not count
export async function kept() {
  const dir = await folder(), out = [];
  for await (const [n, h] of dir.entries()) {
    if (!n.endsWith(".json")) continue;
    try {
      const meta = JSON.parse(await (await h.getFile()).text());
      const file = await (await dir.getFileHandle(key(meta.id) + ".osm.pbf")).getFile();
      if (file.size === meta.size) out.push({ ...meta, file });
    } catch (e) { /* not complete */ }
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

// get an extract ({id, name, pbf}) through the relay into the storage; onProgress(bytes, total); signal: an AbortController's.
// The relay gives Range (206) and the dated file (Content-Location): the file comes in PARTS parts at the same time, each into its
// place in the stored file (one request is slow on some paths of the relay). Without that, one stream, as before. A part that is
// done takes the second half of the part with the most bytes left: one slow part (its first byte came after 9 s on Kentucky) does
// not decide the end. More, smaller parts from the start are slower: each request through the relay waits before its first byte.
const PARTS = 8, TRIES = 4, MIN_PART = 4 << 20, AGAIN_MS = 2000;
export async function download(x, onProgress, signal) {
  const ctl = new AbortController();     // the page's signal, and the stop of all parts when one part fails
  signal?.addEventListener("abort", () => ctl.abort());
  const get = (url, init) => fetch(`${relay}?url=${encodeURIComponent(url)}`, { signal: ctl.signal, ...init });
  const bad = async (r) => new Error(`the relay answered ${r.status}: ${(await r.text()).trim()}`);
  // "<region>-latest.osm.pbf" is a redirect that Geofabrik answers after about 30 s, each time (2026-11, a big region; 502 and 503 under
  // load). A dated file that is there answers in 0.5 s, and one that is not there waits about 30 s. So ask for the files of the last
  // days (UTC, the newest first) at the same time, wait 1.5 s, and take the newest that has answered: it is what "latest" is. If none has
  // answered, the old request for "latest" is the fallback.
  const dated = (back) => x.pbf.replace("-latest.", `-${new Date(Date.now() - back * 864e5).toISOString().slice(2, 10).replace(/-/g, "")}.`);
  const tries = [0, 1, 2, 3, 4].map((back) => {
    const t = { stop: new AbortController(), head: null };
    t.done = get(dated(back), { method: "HEAD", signal: t.stop.signal }).then((h) => (t.head = h), () => null);
    return t;
  });
  await Promise.race([Promise.all(tries.map((t) => t.done)), new Promise((ok) => setTimeout(ok, 1500))]);
  for (const t of tries) t.stop.abort();
  const head = tries.find((t) => t.head?.ok)?.head ?? await get(x.pbf, { method: "HEAD" });
  if (!head.ok) throw await bad(head);
  const total = Number(head.headers.get("Content-Length")) || 0;
  const url = head.headers.get("Content-Location");           // the dated file: all parts are of one version
  const etag = head.headers.get("ETag");
  const parts = head.headers.get("Accept-Ranges") === "bytes" && url ? Math.min(PARTS, Math.floor(total / MIN_PART)) : 0;
  const dir = await folder();
  await dir.removeEntry(key(x.id) + ".json").catch(() => {});      // the old file is not complete from now on
  const out = await (await dir.getFileHandle(key(x.id) + ".osm.pbf", { create: true })).createWritable();
  let got = 0;
  // The answer of a range request; when it has not come in AGAIN_MS, the same request goes again, and the first answer is taken (a
  // request through the relay sometimes waits 9 s for its first byte, and the same request again does not)
  const answer = async (range) => {
    const asks = [0, AGAIN_MS].map((wait) => {
      const stop = new AbortController();
      ctl.signal.addEventListener("abort", () => stop.abort(), { once: true });
      let timer;
      const p = new Promise((ok, no) => { timer = setTimeout(() => get(url, { headers: { Range: range }, signal: stop.signal }).then(ok, no), wait); });
      return { stop, timer, p };
    });
    let won = null;
    const r = await Promise.any(asks.map((a) => a.p.then((res) => {
      if (won) { a.stop.abort(); throw new Error("a later answer"); }     // both answered: the later one stops
      won = a;
      return res;
    })));
    for (const a of asks) if (a !== won) { clearTimeout(a.timer); a.stop.abort(); }
    return r;
  };
  // a range {pos, end} of the file; another part can make its end smaller (take its second half)
  const size = Math.ceil(total / parts);
  const ranges = Array.from({ length: parts }, (_, i) => ({ pos: i * size, end: Math.min(total, (i + 1) * size) - 1 }));
  const fill = async (rg) => {
    for (let t = 1; rg.pos <= rg.end; t++) {
      try {
        const r = await answer(`bytes=${rg.pos}-${rg.end}`);
        if (r.status !== 206) throw await bad(r);
        if (etag && r.headers.get("ETag") !== etag) throw new Error("the file changed during the download");
        const reader = r.body.getReader();
        while (rg.pos <= rg.end) {
          const { done, value } = await reader.read();
          if (done) break;
          const n = Math.min(value.length, rg.end - rg.pos + 1);   // the end can be smaller now: the bytes after it are another part's
          await out.write({ type: "write", position: rg.pos, data: n < value.length ? value.subarray(0, n) : value });
          // another part can have taken the end of this range during the write: the bytes after the new end are the same, and
          // that part writes and counts them
          got += Math.max(0, Math.min(n, rg.end + 1 - rg.pos));
          rg.pos += n;
          onProgress(got, total);
        }
        if (rg.pos <= rg.end) throw new Error("a part ended early");
        reader.cancel().catch(() => {});
      } catch (e) {
        if (ctl.signal.aborted || t >= TRIES) throw e;
        await new Promise((ok) => setTimeout(ok, 500 * t));       // the next try starts at pos: the bytes written stay
      }
    }
  };
  const part = async (rg) => {
    await fill(rg);
    for (;;) {
      // this part is done: take the second half of the range with the most bytes left (at least MIN_PART after its pos)
      const v = ranges.reduce((a, b) => (b.end - b.pos > a.end - a.pos ? b : a));
      const left = v.end - v.pos + 1;
      if (left < 2 * MIN_PART) return;
      const mine = { pos: v.pos + Math.max(MIN_PART, Math.floor(left / 2)), end: v.end };
      v.end = mine.pos - 1;
      ranges.push(mine);
      await fill(mine);
    }
  };
  try {
    if (parts) await Promise.all(ranges.slice().map((rg) => part(rg).catch((e) => { ctl.abort(); throw e; })));
    else {
      const r = await get(x.pbf);
      if (!r.ok) throw await bad(r);
      const reader = r.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        await out.write(value);
        got += value.length;
        onProgress(got, total);
      }
    }
    await out.close();
  } catch (e) {
    await out.abort().catch(() => {});
    throw e;
  }
  if (total && got !== total) throw new Error(`the download stopped after ${got} of ${total} bytes`);
  const meta = { id: x.id, name: x.name, pbf: x.pbf, size: got, modified: head.headers.get("Last-Modified") };
  const w = await (await dir.getFileHandle(key(x.id) + ".json", { create: true })).createWritable();
  await w.write(JSON.stringify(meta));
  await w.close();
  navigator.storage.persist?.();   // ask the browser not to delete the kept files when the disk is full
  return { ...meta, file: await (await dir.getFileHandle(key(x.id) + ".osm.pbf")).getFile() };
}

export async function remove(id) {
  const dir = await folder();
  for (const ext of [".json", ".osm.pbf"]) await dir.removeEntry(key(id) + ext).catch(() => {});
}

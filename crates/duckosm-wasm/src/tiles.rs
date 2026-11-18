//! The build by tiles in the browser (docs/app.md, "The build by tiles"): the steps of `duckosm-rust tiles` (duckOSM
//! docs/design/build_by_tiles.md), each in a worker of its own: the cut of the area file to the tiles ([`tiles_cut`]), the build
//! of each tile ([`tile_build`]) and the union with the steps after it ([`tiles_union`]). The steps are duckOSM's functions
//! of the native command; their files are the files of the page ([`PageFs`], `duckosm_core::fs`): the files that the page gives
//! (`File`s of OPFS, or bytes), and the files that the step writes. A file of the result (a name under `keep`, for example
//! `out/`) goes to an open file of OPFS of a pool (a "slot"); the number of such files follows the number of tables, not the
//! area. All other files (the temporary files of the step, named or not) are blocks of one more open file of OPFS
//! (`duckosm_core::fs::Blocks`), so their number has no limit. Each call gives `{files: {name: slot}}`: the page reads the file
//! of each name from its slot.
//!
//! The steps run with one thread: only the thread that sets the files of the page sees them.

use super::*;
use duckosm_build::{union, Tile};
use std::cell::RefCell;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::rc::Rc;

/// A file that the page gives.
enum Given {
    Blob(Blob),
    Bytes(Rc<Vec<u8>>),
}

/// The files of a step: the files that the page gives (read only), the files of the result that the step writes (in `slots`)
/// and its other files (in `blocks`).
struct PageFs {
    given: RefCell<HashMap<PathBuf, Given>>,
    /// the object of the page with the given files: a file that goes leaves it too, so the browser can free its storage (a `File`
    /// keeps the storage of a deleted file while something points to it)
    given_js: Object,
    own: RefCell<HashMap<PathBuf, usize>>,
    slots: Vec<web_sys::FileSystemSyncAccessHandle>,
    free: RefCell<Vec<usize>>,
    blocks: Rc<duckosm_core::fs::Blocks<OpfsBack>>,
    keep: &'static [&'static str],
}

/// One open file of OPFS as the storage of a block store.
struct OpfsBack(web_sys::FileSystemSyncAccessHandle);

impl duckosm_core::fs::Backing for OpfsBack {
    fn read_at(&self, b: &mut [u8], at: u64) -> io::Result<usize> {
        let a = Uint8Array::new_with_length(b.len() as u32);
        let n = timed(|| self.0.read_with_buffer_source_and_options(&a, &opfs_at(at as f64))).map_err(opfs_err)? as usize;
        a.subarray(0, n as u32).copy_to(&mut b[..n]);
        Ok(n)
    }
    fn write_at(&self, b: &[u8], at: u64) -> io::Result<()> {
        opfs_write(&self.0, at as f64, b).map(|_| ())
    }
}

/// A given file: reads only.
struct ReadOnly<R>(R);

impl<R: Read> Read for ReadOnly<R> {
    fn read(&mut self, b: &mut [u8]) -> io::Result<usize> {
        self.0.read(b)
    }
}

impl<R: Seek> Seek for ReadOnly<R> {
    fn seek(&mut self, to: SeekFrom) -> io::Result<u64> {
        self.0.seek(to)
    }
}

impl<R> Write for ReadOnly<R> {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::Error::other("a file that the page gives is read only"))
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn not_found(p: &Path) -> io::Error {
    io::Error::new(io::ErrorKind::NotFound, format!("{}: no such file", p.display()))
}

impl PageFs {
    /// `given`: an object {name: File or Uint8Array}; `slots`: open files of OPFS for the files of the result (the names under
    /// `keep`); `temp`: the open file of OPFS of the other files.
    fn new(given: &Object, slots: &Array, temp: web_sys::FileSystemSyncAccessHandle, keep: &'static [&'static str]) -> Result<PageFs, JsError> {
        let mut g = HashMap::new();
        for e in Object::entries(given).iter() {
            let e: Array = e.unchecked_into();
            let (name, v) = (e.get(0).as_string().unwrap_or_default(), e.get(1));
            let f = match v.dyn_into::<Uint8Array>() {
                Ok(b) => Given::Bytes(Rc::new(b.to_vec())),
                Err(v) => Given::Blob(v.dyn_into::<Blob>().map_err(|_| JsError::new(&format!("{name}: not a File or bytes")))?),
            };
            g.insert(PathBuf::from(name), f);
        }
        let slots: Vec<web_sys::FileSystemSyncAccessHandle> = slots.iter().map(|h| h.unchecked_into()).collect();
        Ok(PageFs {
            given: RefCell::new(g),
            given_js: given.clone(),
            own: RefCell::new(HashMap::new()),
            free: RefCell::new((0..slots.len()).rev().collect()),
            slots,
            blocks: duckosm_core::fs::Blocks::new(OpfsBack(temp)),
            keep,
        })
    }

    /// The files that the step wrote: {name: slot}.
    fn written(&self) -> Object {
        let o = Object::new();
        for (name, i) in self.own.borrow().iter() {
            let _ = Reflect::set(&o, &name.to_string_lossy().as_ref().into(), &(*i as u32).into());
        }
        o
    }

    fn file(&self, i: usize) -> Box<dyn duckosm_core::fs::Blob> {
        Box::new(OpfsBlob { h: Some(self.slots[i].clone()), pos: 0.0, pool: None })
    }

    /// A given file goes: the page deletes it at once (`{remove: name}`), so its storage is free (the union reads each file of the
    /// tiles one time).
    fn drop_given(&self, p: &Path) -> bool {
        let gone = self.given.borrow_mut().remove(p).is_some();
        if gone {
            let name: JsValue = p.to_string_lossy().as_ref().into();
            let _ = Reflect::delete_property(&self.given_js, &name);
            post(&[("remove", name)]);
        }
        gone
    }
}

impl duckosm_core::fs::Fs for PageFs {
    fn open(&self, p: &Path) -> io::Result<Box<dyn duckosm_core::fs::Blob>> {
        if let Some(&i) = self.own.borrow().get(p) {
            return Ok(self.file(i));
        }
        if self.blocks.len(p).is_some() {
            return Ok(Box::new(self.blocks.named(p, false)?));
        }
        match self.given.borrow().get(p) {
            Some(Given::Blob(b)) => Ok(Box::new(ReadOnly(BlobRead::new(b.clone()).map_err(io::Error::other)?))),
            Some(Given::Bytes(b)) => Ok(Box::new(ReadOnly(io::Cursor::new(b.to_vec())))),
            None => Err(not_found(p)),
        }
    }

    fn create(&self, p: &Path) -> io::Result<Box<dyn duckosm_core::fs::Blob>> {
        if !self.keep.iter().any(|k| p.starts_with(k)) {
            self.drop_given(p);
            return Ok(Box::new(self.blocks.named(p, true)?));
        }
        let had = self.own.borrow().get(p).copied();
        let i = match had {
            Some(i) => i,
            None => {
                self.drop_given(p);
                let i = self.free.borrow_mut().pop().ok_or_else(|| {
                    io::Error::other(format!("{}: all {} open files of OPFS for the files of this step are in use", p.display(), self.slots.len()))
                })?;
                self.own.borrow_mut().insert(p.to_path_buf(), i);
                i
            }
        };
        self.slots[i].truncate_with_f64(0.0).map_err(opfs_err)?;
        Ok(self.file(i))
    }

    fn len(&self, p: &Path) -> io::Result<u64> {
        if let Some(&i) = self.own.borrow().get(p) {
            return Ok(self.slots[i].get_size().map_err(opfs_err)? as u64);
        }
        if let Some(n) = self.blocks.len(p) {
            return Ok(n);
        }
        match self.given.borrow().get(p) {
            Some(Given::Blob(b)) => Ok(b.size() as u64),
            Some(Given::Bytes(b)) => Ok(b.len() as u64),
            None => Err(not_found(p)),
        }
    }

    fn remove_file(&self, p: &Path) -> io::Result<()> {
        let own = self.own.borrow_mut().remove(p);
        if let Some(i) = own {
            self.slots[i].truncate_with_f64(0.0).map_err(opfs_err)?;
            self.free.borrow_mut().push(i);
            return Ok(());
        }
        if self.blocks.remove(p) || self.drop_given(p) {
            Ok(())
        } else {
            Err(not_found(p))
        }
    }

    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        if self.blocks.rename(from, to) {
            return Ok(());
        }
        let i = self.own.borrow_mut().remove(from).ok_or_else(|| not_found(from))?;
        if let Some(old) = self.own.borrow_mut().insert(to.to_path_buf(), i) {
            self.slots[old].truncate_with_f64(0.0).map_err(opfs_err)?;
            self.free.borrow_mut().push(old);
        }
        self.drop_given(to);
        Ok(())
    }

    fn files(&self, dir: &Path) -> Vec<PathBuf> {
        let own = self.own.borrow().keys().filter(|k| k.starts_with(dir)).cloned().collect::<Vec<_>>();
        own.into_iter().chain(self.blocks.files(dir)).chain(self.given.borrow().keys().filter(|k| k.starts_with(dir)).cloned()).collect()
    }
}

/// Run `f` with the files of the page ([`PageFs`]; the unnamed temporary files of `spill` are blocks of `temp` too); then
/// `{files: {name: slot}}` with the fields of `f`'s object.
fn with_page(given: &Object, slots: &Array, temp: web_sys::FileSystemSyncAccessHandle, keep: &'static [&'static str], f: impl FnOnce() -> Result<Object, String>) -> Result<Object, JsError> {
    let fs = Rc::new(PageFs::new(given, slots, temp, keep)?);
    let blocks = fs.blocks.clone();
    duckosm_core::spill::set_temp_store(Some(Rc::new(move || Box::new(duckosm_core::fs::BlockStore(blocks.clone())) as Box<dyn duckosm_core::spill::Store>)));
    duckosm_core::fs::set(Some(fs.clone()));
    // each step of the engine goes to the page at once (`{step, ms, mib}`)
    let _ = duckosm_core::util::STEP_HOOK.set(quack_step);
    QUACK_START.with(|t| t.set(js_sys::Date::now()));
    let res = f();
    duckosm_core::fs::set(None);
    duckosm_core::spill::set_temp_store(None);
    let o = res.map_err(|e| JsError::new(&e))?;
    for (name, i) in fs.own.borrow().iter() {
        fs.slots[*i].flush().map_err(|e| JsError::new(&format!("OPFS: {}: {e:?}", name.display())))?;
    }
    Reflect::set(&o, &"files".into(), &fs.written()).map_err(|_| JsError::new("files"))?;
    Ok(o)
}

/// `[z, x, y]` of a JavaScript array.
fn tile_of(v: &JsValue) -> Result<Tile, String> {
    let a: Array = v.clone().dyn_into().map_err(|_| "a tile is [z, x, y]".to_string())?;
    let n = |i| a.get(i).as_f64().ok_or_else(|| "a tile is [z, x, y]".to_string());
    Ok(Tile { z: n(0)? as u8, x: n(1)? as u32, y: n(2)? as u32 })
}

fn tile_name(t: &Tile) -> String {
    format!("{}_{}_{}", t.z, t.x, t.y)
}

/// The options of a step of the build by tiles: the options of the page ([`page_params`]), the boundary of the area as the given
/// file `boundary.geojson`, and the OSM fixes as `osm_fixes.yaml` when the page gives them. The DEM of `set_dem` becomes the
/// given file of its name (each tile reads the window of its own nodes).
fn tile_params(fs: &Object, modes: &str, raw: bool, levels: Option<&str>) -> Result<Params, String> {
    let mut p = page_params(modes, raw, levels)?;
    p.boundary = Some("boundary.geojson".into());
    if Reflect::has(fs, &"osm_fixes.yaml".into()).unwrap_or(false) {
        p.osm_fixes = Some("osm_fixes.yaml".into());
    }
    if let Some((name, bytes)) = p.dem_bytes.take() {
        Reflect::set(fs, &name.as_str().into(), &Uint8Array::from(&bytes[..])).map_err(|_| "the DEM")?;
        p.dem = Some(name.into());
    }
    Ok(p)
}

/// The tiles of zoom `zoom` of the boundary (GeoJSON text), `[[z, x, y], ...]`: the tiles that [`tiles_cut`] can give a file
/// (the page opens two files of OPFS for each).
#[wasm_bindgen]
pub fn tile_list(boundary: &str, zoom: u8) -> Result<Array, JsError> {
    let b = Boundary::parse(boundary).map_err(|e| JsError::new(&e))?;
    Ok(duckosm_core::tiles::tiles(&b, zoom).iter().map(|t| JsValue::from(Array::of3(&t.z.into(), &t.x.into(), &t.y.into()))).collect())
}

/// Step 1 of the build by tiles: cut the area file (the given file `area.osm.pbf`) to the tiles of zoom `zoom` of the boundary
/// (the given file `boundary.geojson`), as `duckosm-rust tiles` does (`union::cut_tiles`): the files `tiles/z_x_y.osm.pbf` and
/// `tiles/z_x_y.junctions`. The multipolygons and the border of each tile follow the options (`modes`, `raw`). Gives
/// `{tiles: [[z, x, y], ...] (the tiles with a file), files}`.
#[wasm_bindgen]
pub fn tiles_cut(given: Object, zoom: u8, modes: &str, raw: bool, slots: Array, temp: web_sys::FileSystemSyncAccessHandle) -> Result<Object, JsError> {
    let text = Reflect::get(&given, &"boundary.geojson".into()).ok().and_then(|b| b.dyn_into::<Uint8Array>().ok()).map(|b| String::from_utf8_lossy(&b.to_vec()).into_owned());
    let text = text.ok_or_else(|| JsError::new("tiles_cut: the boundary (boundary.geojson) as bytes"))?;
    let p = page_params(modes, raw, None).map_err(|e| JsError::new(&e))?;
    with_page(&given, &slots, temp, &["tiles/"], || {
        let multipolygons = p.raw || p.features || p.building_faces;
        let tiles = union::cut_tiles(Path::new("area.osm.pbf"), &text, zoom, multipolygons, union::border_m(&p), Path::new("tiles"))?;
        let list = Array::new();
        for t in &tiles {
            list.push(&Array::of3(&t.z.into(), &t.x.into(), &t.y.into()));
        }
        let o = Object::new();
        Reflect::set(&o, &"tiles".into(), &list).map_err(|_| "tiles")?;
        Ok(o)
    })
}

/// Step 2: the build of one tile (`duckosm_build::build` with `--tile`, as each tile process of `duckosm-rust tiles`): from the
/// given files `tiles/z_x_y.osm.pbf` and `tiles/z_x_y.junctions` (and `sidewalks.all`, the roads of the sidewalk step), its
/// tables in `tiles/z_x_y/`. With `sidewalks_step`, only the sidewalk step of the tile (`tiles/z_x_y/sidewalks.bin`; it reads
/// the files of the tiles near it too). Gives `{summary, files}`.
#[wasm_bindgen]
pub fn tile_build(given: Object, tile: JsValue, modes: &str, raw: bool, sidewalks_step: bool, slots: Array, temp: web_sys::FileSystemSyncAccessHandle) -> Result<Object, JsError> {
    let t = tile_of(&tile).map_err(|e| JsError::new(&e))?;
    let mut p = tile_params(&given, modes, raw, Some("none")).map_err(|e| JsError::new(&e))?;
    let name = tile_name(&t);
    p.tile = Some(t);
    p.outside_junctions = Some(format!("tiles/{name}.junctions").into());
    p.sidewalks = Reflect::has(&given, &"sidewalks.all".into()).unwrap_or(false).then(|| "sidewalks.all".into());
    p.sidewalks_step = sidewalks_step;
    p.geoparquet = true;
    p.duckdb = false;
    with_page(&given, &slots, temp, &["tiles/"], || {
        let sum = duckosm_build::build(Path::new(&format!("tiles/{name}.osm.pbf")), Path::new(&format!("tiles/{name}")), &p)?;
        let o = Object::new();
        Reflect::set(&o, &"summary".into(), &sum.lines().join("\n").into()).map_err(|_| "summary")?;
        Ok(o)
    })
}

/// Step 3: the union of the tables of the tiles (the given files `tiles/z_x_y/*`, each tile of `tiles`) and the steps after it
/// (the bus routes, the levels of `levels`, the tables of the whole area), as `duckosm-rust tiles` does (`union::union_all`):
/// the tables in `out/`. Each file of a tile goes when the union has read it. Gives `{summary, files}`: the summary has the
/// lines of the whole build for the edges and the nodes of each mode.
#[wasm_bindgen]
pub fn tiles_union(given: Object, tiles: Array, modes: &str, raw: bool, levels: Option<String>, slots: Array, temp: web_sys::FileSystemSyncAccessHandle) -> Result<Object, JsError> {
    let p = tile_params(&given, modes, raw, levels.as_deref()).map_err(|e| JsError::new(&e))?;
    let text = Reflect::get(&given, &"boundary.geojson".into()).ok().and_then(|b| b.dyn_into::<Uint8Array>().ok()).map(|b| String::from_utf8_lossy(&b.to_vec()).into_owned());
    let text = text.ok_or_else(|| JsError::new("tiles_union: the boundary (boundary.geojson) as bytes"))?;
    let built = tiles
        .iter()
        .map(|v| tile_of(&v).map(|t| (t, PathBuf::from(format!("tiles/{}", tile_name(&t))))))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| JsError::new(&e))?;
    with_page(&given, &slots, temp, &["out/"], || {
        let rows = union::union_all(&built, &text, &p, true, Path::new("out"))?;
        let count = |name: String| rows.iter().find(|r| r.0 == name).map_or(0, |r| r.1);
        // the lines of the whole build (`Summary::lines`) that the page reads: the edges and the nodes of each mode
        let lines: Vec<String> = p
            .modes
            .iter()
            .map(|m| {
                let n = |t: &str| count(format!("{t}_{}", m.name()));
                format!(
                    "{}: {} edges, {} nodes, {} private edges, {} edge_id_map rows, {} edge_graph rows, {} turn restrictions",
                    m.name(),
                    n("edges"),
                    n("nodes"),
                    n("private_edges"),
                    n("edge_id_map"),
                    n("edge_graph"),
                    n("turn_restrictions")
                )
            })
            .collect();
        let o = Object::new();
        Reflect::set(&o, &"summary".into(), &lines.join("\n").into()).map_err(|_| "summary")?;
        let r = Object::new();
        for (name, n) in &rows {
            let _ = Reflect::set(&r, &name.as_str().into(), &(*n as f64).into());
        }
        Reflect::set(&o, &"rows".into(), &r).map_err(|_| "rows")?;
        Ok(o)
    })
}

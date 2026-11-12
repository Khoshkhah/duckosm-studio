//! duckOSM Studio, the browser app (docs/browser.md): the bytes of a `.osm.pbf` file in, duckOSM's tables of the
//! chosen modes out, as GeoParquet bytes. The steps are `duckosm_build::run`, as in the native build.

use duckosm_build::{
    config::parse_osm_fixes, parse_levels, run, Input, Params, StepLog, FEATURES_SQL,
};
use duckosm_core::boundary::Boundary;
use duckosm_core::filter::Mode;
use js_sys::{Array, Object, Reflect, Uint8Array};
use std::io::{self, BufWriter, Read, Seek, SeekFrom, Write};
use wasm_bindgen::prelude::*;

#[cfg(feature = "threads")]
mod alloc;
#[cfg(feature = "threads")]
#[global_allocator]
static ALLOC: alloc::Cached = alloc::Cached;

/// `initThreadPool(n)` in JavaScript: starts n Web Workers for rayon (the build with the feature "threads").
#[cfg(feature = "threads")]
pub use wasm_bindgen_rayon::init_thread_pool;
use web_sys::{Blob, FileReaderSync};

/// A panic stops the module with only "RuntimeError: unreachable". The hook keeps the panic message in
/// `globalThis.duckosmPanic`, so the build worker can show it.
#[wasm_bindgen(start)]
fn keep_panic_message() {
    std::panic::set_hook(Box::new(|info| {
        let _ = Reflect::set(
            &js_sys::global(),
            &"duckosmPanic".into(),
            &info.to_string().into(),
        );
    }));
}

/// The result of [`build`]. `files` maps each file name (for example `edges_driving.parquet`) to an array
/// of `Uint8Array` parts: `new Blob(files[name])` gives the file.
#[wasm_bindgen(getter_with_clone)]
pub struct Output {
    pub files: Object,
    /// The result as lines of text (`Summary::lines`): the numbers of rows of each mode, and more.
    pub summary: String,
    /// The time and the memory of each step, JSON: `[{"step": name, "ms": time, "mib": WebAssembly memory after it}]`.
    pub log: String,
}

/// Milliseconds: the browser's clock in WebAssembly (std has no clock there), the system's natively (the tests).
#[cfg(target_arch = "wasm32")]
fn now_ms() -> f64 {
    js_sys::Date::now()
}

#[cfg(not(target_arch = "wasm32"))]
fn now_ms() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64() * 1000.0)
}

/// The WebAssembly memory in MiB. It only grows, so it is the largest size until now. 0 natively.
#[cfg(target_arch = "wasm32")]
fn memory_mib() -> f64 {
    (core::arch::wasm32::memory_size(0) * 65536) as f64 / 1048576.0
}

#[cfg(not(target_arch = "wasm32"))]
fn memory_mib() -> f64 {
    0.0
}

/// Sends the bytes to a JavaScript array part by part, so the files are not kept in the WebAssembly
/// memory (at most 4 GB). The edges file of Kentucky is 400 MB.
struct JsParts(Array);

// SAFETY: ArrowWriter needs Send. The tables are written on the calling thread, also in the build with
// threads (only the PBF reader uses rayon), so the JavaScript array never moves to another thread.
unsafe impl Send for JsParts {}

impl Write for JsParts {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.push(&Uint8Array::from(buf));
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Reads a browser `Blob` (a `File` that the user chose) piece by piece, so the region file is never in the
/// WebAssembly memory as a whole. `FileReaderSync` exists only in a worker: the page builds in one. It reads 4 MB
/// at a time (one call to the browser costs more than the copy) and keeps them, so a seek into these 4 MB
/// reads nothing more.
struct BlobRead {
    blob: Blob,
    reader: FileReaderSync,
    pos: u64,
    buf: Vec<u8>,
    buf_at: u64,
}

impl BlobRead {
    fn new(blob: Blob) -> Result<BlobRead, String> {
        Ok(BlobRead {
            blob,
            reader: FileReaderSync::new().map_err(|e| format!("{e:?}"))?,
            pos: 0,
            buf: Vec::new(),
            buf_at: 0,
        })
    }
}

// SAFETY: the cut needs Send. Step 0 (`cutstream`) reads the blocks on the calling thread and gives only the read
// bytes to rayon, so the browser objects never move to another thread, also in the build with threads.
unsafe impl Send for BlobRead {}

impl Read for BlobRead {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        let size = self.blob.size() as u64;
        if self.pos >= size || out.is_empty() {
            return Ok(0);
        }
        if self.pos < self.buf_at || self.pos >= self.buf_at + self.buf.len() as u64 {
            let end = (self.pos + (4 << 20)).min(size);
            let js = |e: JsValue| io::Error::other(format!("{e:?}"));
            let part = self
                .blob
                .slice_with_f64_and_f64(self.pos as f64, end as f64)
                .map_err(js)?;
            self.buf =
                Uint8Array::new(&self.reader.read_as_array_buffer(&part).map_err(js)?.into())
                    .to_vec();
            self.buf_at = self.pos;
        }
        let from = (self.pos - self.buf_at) as usize;
        let n = out.len().min(self.buf.len() - from);
        out[..n].copy_from_slice(&self.buf[from..from + n]);
        self.pos += n as u64;
        Ok(n)
    }
}

impl Seek for BlobRead {
    fn seek(&mut self, to: SeekFrom) -> io::Result<u64> {
        self.pos = match to {
            SeekFrom::Start(n) => n,
            SeekFrom::Current(d) => self.pos.saturating_add_signed(d),
            SeekFrom::End(d) => (self.blob.size() as u64).saturating_add_signed(d),
        };
        Ok(self.pos)
    }
}

/// duckOSM's SQL of the base-map layers (`features.*`): it loads the `raw_*.parquet` files of [`build_area`] with
/// `raw` into `raw.*` and makes the layers. It needs DuckDB's spatial extension (docs/features.md).
#[wasm_bindgen]
pub fn features_sql() -> String {
    FEATURES_SQL.to_string()
}

/// Build duckOSM's tables of an area: `region` is a `.osm.pbf` file (a `File` or `Blob`) that covers the
/// `boundary` (GeoJSON, as `duckosm boundary` writes it). Step 0 cuts it to the area file (in memory), and the build
/// reads the area file. With
/// `raw`, the output also has `raw_nodes.parquet`, `raw_ways.parquet` and `raw_relations.parquet`: duckOSM's
/// `raw.*` of the area, cut as osmium's `smart` does, the input of [`features_sql`], and the cut itself as
/// `cut.osm.pbf` (the file for the user to download).
#[wasm_bindgen]
pub fn build_area(
    region: Blob,
    boundary: &str,
    modes: &str,
    fixes_yaml: Option<String>,
    raw: Option<bool>,
    levels: Option<String>,
) -> Result<Output, JsError> {
    let text = boundary;
    let boundary = Boundary::parse(boundary).map_err(|e| JsError::new(&e))?;
    let area = cut(|| BlobRead::new(region.clone()), &boundary)?;
    let open = || -> Result<Box<dyn Read + Send + '_>, String> { Ok(Box::new(io::Cursor::new(&area[..]))) };
    js_build(
        Input::Area(&open, &boundary),
        modes,
        fixes_yaml,
        Some(text),
        raw.unwrap_or(false),
        levels,
    )
}

/// Step 0: cut the region file to the area file (a `.osm.pbf` file), with only lists of ids in memory.
// ponytail: the area file stays in memory (Kentucky: 149 MB); a file in OPFS keeps it out when an area needs it
fn cut<R: Read + Seek + Send>(open: impl Fn() -> Result<R, String>, boundary: &Boundary) -> Result<Vec<u8>, JsError> {
    let (area, _) = duckosm_core::cutstream::cut_to_pbf(open, boundary, Vec::new()).map_err(|e| JsError::new(&e))?;
    Ok(area)
}

/// Step 0 in OPFS: cut the region file `region` (a `File` or `Blob`) to the `boundary` and write the area file through
/// `out`, a `FileSystemSyncAccessHandle` that the worker opened. Then [`build_area_file`] reads the area file. Gives the
/// size of the area file in bytes.
#[wasm_bindgen]
pub fn cut_area(region: Blob, boundary: &str, out: web_sys::FileSystemSyncAccessHandle) -> Result<f64, JsError> {
    let boundary = Boundary::parse(boundary).map_err(|e| JsError::new(&e))?;
    let file = BufWriter::with_capacity(1 << 20, OpfsFile { h: out, at: 0.0 });
    let (w, _) = duckosm_core::cutstream::cut_to_pbf(|| BlobRead::new(region.clone()), &boundary, file)
        .map_err(|e| JsError::new(&e))?;
    let f = w.into_inner().map_err(|e| JsError::new(&e.to_string()))?;
    f.h.flush().map_err(|e| JsError::new(&format!("OPFS: {e:?}")))?;
    Ok(f.at)
}

/// Build duckOSM's tables of an area from its area file (`area`: the file of [`cut_area`], as a `File`). `spill` is a
/// pool of open files in OPFS (`FileSystemSyncAccessHandle`) for the temporary files of the network step, so they are
/// not in the WebAssembly memory (Kentucky: at most 11 at once). The other arguments are those of [`build_area`].
#[wasm_bindgen]
pub fn build_area_file(
    area: Blob,
    boundary: &str,
    modes: &str,
    fixes_yaml: Option<String>,
    raw: Option<bool>,
    levels: Option<String>,
    spill: Array,
) -> Result<Output, JsError> {
    let text = boundary;
    let boundary = Boundary::parse(boundary).map_err(|e| JsError::new(&e))?;
    let open = || -> Result<Box<dyn Read + Send>, String> {
        Ok(Box::new(io::BufReader::with_capacity(1 << 20, BlobRead::new(area.clone())?)))
    };
    let pool = std::rc::Rc::new(OpfsPool {
        free: std::cell::RefCell::new(spill.iter().map(|h| h.unchecked_into()).collect()),
    });
    if spill.length() > 0 {
        duckosm_core::spill::set_temp_store(Some(std::rc::Rc::new(move || {
            Box::new(OpfsStore(pool.clone())) as Box<dyn duckosm_core::spill::Store>
        })));
    }
    let res = js_build(Input::Area(&open, &boundary), modes, fixes_yaml, Some(text), raw.unwrap_or(false), levels);
    duckosm_core::spill::set_temp_store(None);
    res
}

fn opfs_err(e: JsValue) -> io::Error {
    io::Error::other(format!("OPFS: {e:?}"))
}

/// The options of a read or a write at `at` bytes.
fn opfs_at(at: f64) -> web_sys::FileSystemReadWriteOptions {
    let o = web_sys::FileSystemReadWriteOptions::new();
    o.set_at(at);
    o
}

/// Write `b` at `at`. The bytes are copied out of the WebAssembly memory first: the browser does not write from a view
/// of the shared memory of the build with threads. A full storage quota gives a short count (Chromium: -8).
fn opfs_write(h: &web_sys::FileSystemSyncAccessHandle, at: f64, b: &[u8]) -> io::Result<usize> {
    let a = Uint8Array::new_with_length(b.len() as u32);
    a.copy_from(b);
    let n = h.write_with_buffer_source_and_options(&a, &opfs_at(at)).map_err(opfs_err)?;
    if n != b.len() as f64 {
        return Err(io::Error::other(format!(
            "OPFS: wrote {n} of {} bytes: the storage of the browser is full, or the site may not use more of it",
            b.len()
        )));
    }
    Ok(b.len())
}

/// A file in OPFS, written from the start through its `FileSystemSyncAccessHandle`.
struct OpfsFile {
    h: web_sys::FileSystemSyncAccessHandle,
    at: f64,
}

// SAFETY: the handle is used only on the thread that opened it (the build worker): the cut writes on the calling
// thread, and only the encoding of the blocks runs on the other threads.
unsafe impl Send for OpfsFile {}

impl Write for OpfsFile {
    fn write(&mut self, b: &[u8]) -> io::Result<usize> {
        let n = opfs_write(&self.h, self.at, b)?;
        self.at += n as f64;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.h.flush().map_err(opfs_err)
    }
}

/// The temporary files of the network step in OPFS. A file of OPFS opens only asynchronously, and the network opens
/// its temporary files in synchronous code: so the worker opens a pool of files before the build, and the store gives
/// them out and takes them back, empty, when a temporary file is dropped.
struct OpfsPool {
    free: std::cell::RefCell<Vec<web_sys::FileSystemSyncAccessHandle>>,
}

struct OpfsStore(std::rc::Rc<OpfsPool>);

struct OpfsBlob {
    h: Option<web_sys::FileSystemSyncAccessHandle>,
    pos: f64,
    pool: std::rc::Rc<OpfsPool>,
}

// SAFETY: the store is set for the calling thread only (`spill::set_temp_store`), so the handles stay on the thread
// that opened them.
unsafe impl Send for OpfsBlob {}

impl duckosm_core::spill::Store for OpfsStore {
    fn create(&self) -> io::Result<Box<dyn duckosm_core::spill::Blob>> {
        let h = self.0.free.borrow_mut().pop().ok_or_else(|| {
            io::Error::other("OPFS: all files of the pool for the temporary files are in use")
        })?;
        h.truncate_with_f64(0.0).map_err(opfs_err)?;
        Ok(Box::new(OpfsBlob { h: Some(h), pos: 0.0, pool: self.0.clone() }))
    }
}

impl Drop for OpfsBlob {
    fn drop(&mut self) {
        if let Some(h) = self.h.take() {
            let _ = h.truncate_with_f64(0.0);
            self.pool.free.borrow_mut().push(h);
        }
    }
}

impl OpfsBlob {
    fn h(&self) -> &web_sys::FileSystemSyncAccessHandle {
        self.h.as_ref().expect("the handle is there until the drop")
    }
}

impl Read for OpfsBlob {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let a = Uint8Array::new_with_length(buf.len() as u32);
        let n = self.h().read_with_buffer_source_and_options(&a, &opfs_at(self.pos)).map_err(opfs_err)? as usize;
        a.subarray(0, n as u32).copy_to(&mut buf[..n]);
        self.pos += n as f64;
        Ok(n)
    }
}

impl Write for OpfsBlob {
    fn write(&mut self, b: &[u8]) -> io::Result<usize> {
        let n = opfs_write(self.h(), self.pos, b)?;
        self.pos += n as f64;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.h().flush().map_err(opfs_err)
    }
}

impl Seek for OpfsBlob {
    fn seek(&mut self, to: SeekFrom) -> io::Result<u64> {
        self.pos = match to {
            SeekFrom::Start(x) => x as f64,
            SeekFrom::End(x) => self.h().get_size().map_err(opfs_err)? + x as f64,
            SeekFrom::Current(x) => self.pos + x as f64,
        };
        Ok(self.pos as u64)
    }
}

/// The same as [`build_area`], with the bytes of the region file (for Node, which has no `FileReaderSync`).
#[wasm_bindgen]
pub fn build_area_bytes(
    region: &[u8],
    boundary: &str,
    modes: &str,
    fixes_yaml: Option<String>,
    raw: Option<bool>,
    levels: Option<String>,
) -> Result<Output, JsError> {
    let text = boundary;
    let boundary = Boundary::parse(boundary).map_err(|e| JsError::new(&e))?;
    let area = cut(|| Ok(io::Cursor::new(region)), &boundary)?;
    let open = || -> Result<Box<dyn Read + Send + '_>, String> { Ok(Box::new(io::Cursor::new(&area[..]))) };
    js_build(
        Input::Area(&open, &boundary),
        modes,
        fixes_yaml,
        Some(text),
        raw.unwrap_or(false),
        levels,
    )
}

/// Build duckOSM's tables of `modes` (comma separated: driving, walking, cycling) from the bytes of a `.osm.pbf`
/// file, with the default options of the CLI. `fixes_yaml`: the text of an OSM fixes file (duckOSM
/// `osm_overrides`), or nothing. `boundary`: the boundary of the area (GeoJSON), only for `main.boundary` and
/// the metadata: the file is cut already.
#[wasm_bindgen]
pub fn build(
    pbf_bytes: &[u8],
    modes: &str,
    fixes_yaml: Option<String>,
    boundary: Option<String>,
    levels: Option<String>,
) -> Result<Output, JsError> {
    js_build(
        Input::Pbf(pbf_bytes),
        modes,
        fixes_yaml,
        boundary.as_deref(),
        false,
        levels,
    )
}

/// The same as [`build`], from OSM JSON: the answer of the Overpass API to the query of the page (the highway
/// ways and ferry lines with their nodes, and the `type=restriction` relations). With a `boundary` (GeoJSON),
/// only the ways with a node inside are kept, as in [`build_area`]. With `raw`, the output also has the
/// `raw_*.parquet` files of [`build_area`] (cut as osmium's `smart` does), from the objects of the base-map layers
/// that the query also asked for.
#[wasm_bindgen]
pub fn build_osm_json(
    json: &[u8],
    boundary: Option<String>,
    modes: &str,
    fixes_yaml: Option<String>,
    levels: Option<String>,
    raw: Option<bool>,
) -> Result<Output, JsError> {
    let parsed = boundary
        .as_deref()
        .map(Boundary::parse)
        .transpose()
        .map_err(|e| JsError::new(&e))?;
    js_build(
        Input::OsmJson(json, parsed.as_ref()),
        modes,
        fixes_yaml,
        boundary.as_deref(),
        raw.unwrap_or(false),
        levels,
    )
}

thread_local! {
    /// Whether the next build with the layers makes the walls of the buildings (`set_building_faces`; on when the page says nothing).
    static BUILDING_FACES: std::cell::Cell<bool> = const { std::cell::Cell::new(true) };
}

/// Whether the builds after this call make `main.building_faces`, the walls of the buildings, each with a type (the page's checkbox
/// "Wall labels"). The walls are made only with the base-map layers (`raw`): they need the buildings. On by default.
#[wasm_bindgen]
pub fn set_building_faces(on: bool) {
    BUILDING_FACES.with(|b| b.set(on));
}

thread_local! {
    /// The time zone table entry of the next build (`set_timezone_entry`).
    static TIMEZONE_ENTRY: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
}

/// The entry of the area's Geofabrik region in the page's time zone table (`www/timezones.json`), as JSON text,
/// for the builds after this call: the time zone of `main.visualization_metadata`. The browser build has no world
/// map of the zones; without an entry, the time zone is NULL.
#[wasm_bindgen]
pub fn set_timezone_entry(entry: Option<String>) {
    TIMEZONE_ENTRY.with(|e| *e.borrow_mut() = entry);
}

thread_local! {
    /// The DEM of the next build (`set_dem`): the file name and the bytes of a GeoTIFF.
    static DEM: std::cell::RefCell<Option<(String, Vec<u8>)>> = const { std::cell::RefCell::new(None) };
    /// What the page says of that DEM (`set_dem`): product, vertical datum, license, and whether it is not the reference.
    static DEM_INFO: std::cell::RefCell<(Option<String>, Option<String>, Option<String>, bool)> = const { std::cell::RefCell::new((None, None, None, false)) };
}

/// The DEM (a GeoTIFF, EPSG:4326) for the next build: its file name and its bytes. The build then writes
/// `elevation_*` and `main.elevation_grid`, as `duckosm build --dem` does; `main.elevation_metadata` has the file
/// name as the source. Without a name or without bytes, the next build has no DEM. The build takes the DEM, so
/// each build needs its own call. The area's reference ground needs its `product` (`DTM` or `DSM`), its `vertical_datum`
/// and its `license`; a DEM that is not the reference (`not_reference`) can leave them out (they are then `unknown`).
#[wasm_bindgen]
pub fn set_dem(
    name: Option<String>,
    bytes: Option<Vec<u8>>,
    product: Option<String>,
    vertical_datum: Option<String>,
    license: Option<String>,
    not_reference: Option<bool>,
) {
    DEM.with(|d| *d.borrow_mut() = name.zip(bytes));
    DEM_INFO.with(|d| *d.borrow_mut() = (product, vertical_datum, license, not_reference.unwrap_or(false)));
}

fn js_build(
    input: Input,
    modes: &str,
    fixes_yaml: Option<String>,
    boundary: Option<&str>,
    raw: bool,
    levels: Option<String>,
) -> Result<Output, JsError> {
    let e = |e: String| JsError::new(&e);
    let (dem_product, dem_vertical_datum, dem_license, dem_not_reference) = DEM_INFO.with(|d| d.borrow_mut().clone());
    let mut p = Params {
        modes: Mode::parse_all(&modes.split(',').collect::<Vec<_>>()).map_err(e)?,
        raw,
        cut_pbf: raw && matches!(input, Input::Area(..)), // the cut file: only of a region file
        building_faces: raw && BUILDING_FACES.with(|b| b.get()), // with the layers, and "Wall labels" on: the walls of the buildings (main.building_faces)
        built_at: Some((js_sys::Date::now() * 1000.0) as i64),
        timezone_entry: TIMEZONE_ENTRY.with(|e| e.borrow().clone()),
        dem_bytes: DEM.with(|d| d.borrow_mut().take()),
        dem_product,
        dem_vertical_datum,
        dem_license,
        dem_not_reference,
        ..Params::default()
    };
    if let Some(m) = levels.as_deref() {
        p.levels = parse_levels(m).map_err(e)?;
    }
    let fixes = match fixes_yaml {
        Some(text) => parse_osm_fixes(&text, "the OSM fixes file").map_err(e)?,
        None => Default::default(),
    };
    let files = Object::new();
    let mut log = StepLog::new(now_ms, memory_mib);
    let sum = run(input, &fixes, boundary, &p, &mut log, |name| {
        let parts = Array::new();
        Reflect::set(&files, &name.into(), &parts).map_err(|_| format!("cannot keep {name}"))?;
        // the array is a JavaScript object: the writer and `files` share it
        Ok(BufWriter::with_capacity(1 << 20, JsParts(parts)))
    })
    .map_err(e)?;
    Ok(Output {
        files,
        summary: sum.lines().join("\n"),
        log: log.json(),
    })
}

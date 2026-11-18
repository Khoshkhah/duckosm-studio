//! The GeoPackage of quack --gdal in OPFS: a SQLite VFS that writes the main database file directly into an open
//! file of OPFS (a `FileSystemSyncAccessHandle`). There is no copy of the file in memory and no second file: the
//! GeoPackage is a normal `.gpkg` file in the folder of the run. The writer sets `journal_mode = OFF`, so SQLite
//! opens no journal. Other files of SQLite (temporary files) stay in memory.

use sqlite_wasm_rs::utils::{
    ffi, MemChunksFile, OsCallback, SQLiteIoMethods, SQLiteVfs, SQLiteVfsFile, VfsError, VfsFile, VfsResult, VfsStore,
};
use sqlite_wasm_rs::WasmOsCallback;
use std::cell::RefCell;
use std::collections::HashMap;
use web_sys::FileSystemSyncAccessHandle;

thread_local! {
    /// The OPFS file for the next main database that SQLite opens.
    static NEXT: RefCell<Option<FileSystemSyncAccessHandle>> = const { RefCell::new(None) };
    /// The open files of this VFS, by name.
    static FILES: RefCell<HashMap<String, File>> = RefCell::new(HashMap::new());
}

enum File {
    Opfs(FileSystemSyncAccessHandle),
    Mem(MemChunksFile),
}

fn err(e: wasm_bindgen::JsValue) -> VfsError {
    VfsError::new(ffi::SQLITE_IOERR, format!("OPFS: {e:?}"))
}

fn at(at: usize) -> web_sys::FileSystemReadWriteOptions {
    let o = web_sys::FileSystemReadWriteOptions::new();
    o.set_at(at as f64);
    o
}

impl VfsFile for File {
    fn read(&self, buf: &mut [u8], offset: usize) -> VfsResult<bool> {
        match self {
            File::Mem(f) => f.read(buf, offset),
            File::Opfs(h) => {
                // a copy: OPFS does not read into the shared memory of the build with threads
                let a = js_sys::Uint8Array::new_with_length(buf.len() as u32);
                let n = h.read_with_buffer_source_and_options(&a, &at(offset)).map_err(err)? as usize;
                a.subarray(0, n as u32).copy_to(&mut buf[..n]);
                if n < buf.len() {
                    buf[n..].fill(0);
                }
                Ok(n == buf.len())
            }
        }
    }

    fn write(&mut self, buf: &[u8], offset: usize) -> VfsResult<()> {
        match self {
            File::Mem(f) => f.write(buf, offset),
            File::Opfs(h) => {
                let a = js_sys::Uint8Array::new_with_length(buf.len() as u32);
                a.copy_from(buf);
                let n = h.write_with_buffer_source_and_options(&a, &at(offset)).map_err(err)? as usize;
                if n != buf.len() {
                    return Err(VfsError::new(
                        ffi::SQLITE_FULL,
                        format!("OPFS: wrote {n} of {} bytes: the storage of the browser is full", buf.len()),
                    ));
                }
                Ok(())
            }
        }
    }

    fn truncate(&mut self, size: usize) -> VfsResult<()> {
        match self {
            File::Mem(f) => f.truncate(size),
            File::Opfs(h) => h.truncate_with_f64(size as f64).map_err(err),
        }
    }

    fn flush(&mut self) -> VfsResult<()> {
        match self {
            File::Mem(f) => f.flush(),
            File::Opfs(h) => h.flush().map_err(err),
        }
    }

    fn size(&self) -> VfsResult<usize> {
        match self {
            File::Mem(f) => f.size(),
            File::Opfs(h) => Ok(h.get_size().map_err(err)? as usize),
        }
    }
}

struct Store;

impl VfsStore<File, ()> for Store {
    fn add_file(_: *mut ffi::sqlite3_vfs, name: &str, flags: i32) -> VfsResult<()> {
        let file = if flags & ffi::SQLITE_OPEN_MAIN_DB != 0 {
            let h = NEXT.with_borrow_mut(Option::take)
                .ok_or_else(|| VfsError::new(ffi::SQLITE_CANTOPEN, format!("{name}: no OPFS file for the GeoPackage")))?;
            h.truncate_with_f64(0.0).map_err(err)?;
            File::Opfs(h)
        } else {
            File::Mem(MemChunksFile::default())
        };
        FILES.with_borrow_mut(|m| m.insert(name.into(), file));
        Ok(())
    }

    fn contains_file(_: *mut ffi::sqlite3_vfs, name: &str) -> VfsResult<bool> {
        Ok(FILES.with_borrow(|m| m.contains_key(name)))
    }

    fn delete_file(_: *mut ffi::sqlite3_vfs, name: &str) -> VfsResult<()> {
        FILES.with_borrow_mut(|m| m.remove(name));
        Ok(())
    }

    fn with_file<F: Fn(&File) -> VfsResult<i32>>(f: &SQLiteVfsFile, op: F) -> VfsResult<i32> {
        let name = unsafe { f.name() };
        FILES.with_borrow(|m| op(m.get(name).ok_or_else(|| VfsError::new(ffi::SQLITE_IOERR, format!("{name}: not open")))?))
    }

    fn with_file_mut<F: Fn(&mut File) -> VfsResult<i32>>(f: &SQLiteVfsFile, op: F) -> VfsResult<i32> {
        let name = unsafe { f.name() };
        FILES.with_borrow_mut(|m| op(m.get_mut(name).ok_or_else(|| VfsError::new(ffi::SQLITE_IOERR, format!("{name}: not open")))?))
    }
}

struct Io;

impl SQLiteIoMethods for Io {
    type File = File;
    type AppData = ();
    type Store = Store;
    const VERSION: i32 = 1;
}

struct Vfs;

impl SQLiteVfs<Io> for Vfs {
    const VERSION: i32 = 1;
    fn sleep(d: std::time::Duration) {
        WasmOsCallback::sleep(d)
    }
    fn random(b: &mut [u8]) {
        WasmOsCallback::random(b)
    }
    fn epoch_timestamp_in_ms() -> i64 {
        WasmOsCallback::epoch_timestamp_in_ms()
    }
}

/// Make `h` the file of the next main database that SQLite opens (the GeoPackage). Registers this VFS as SQLite's
/// default VFS the first time.
pub fn open_next(h: FileSystemSyncAccessHandle) -> Result<(), String> {
    const NAME: &str = "opfs-gpkg";
    if sqlite_wasm_rs::utils::registered_vfs(NAME).map_err(|e| e.to_string())?.is_none() {
        sqlite_wasm_rs::utils::register_vfs::<Io, Vfs>(NAME, (), true).map_err(|e| format!("the GeoPackage VFS: {e}"))?;
    }
    NEXT.set(Some(h));
    Ok(())
}

/// Forget the files of this VFS (after the GeoPackage is closed).
pub fn close() {
    NEXT.set(None);
    FILES.with_borrow_mut(HashMap::clear);
}

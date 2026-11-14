//! The allocator of the build with threads. The allocator of the standard library in WebAssembly has one global
//! lock, and a thread that waits for it spins. The decode of a block of ways makes millions of small allocations,
//! so with 4 threads, pass 2 of the cut took 2.2 s instead of 1.2 s with one thread. Here each thread keeps its
//! own lists of free small blocks (up to 256 bytes) and takes a lock only for a batch of 64 blocks at a time.
//!
//! A small block goes back to the list of the thread that frees it. A thread keeps at most `KEEP` free blocks of a
//! size: the blocks over that go, a batch of 64 at a time, to a shared pool, and a thread with no free block takes a
//! batch from the pool before it asks the system for new memory. Without the pool, the free blocks stayed with the
//! thread that freed them: the threads that made the rows of quack always took new memory, the thread that wrote the
//! rows kept the freed blocks, and quack by tiles of Île-de-France filled the 4 GiB in 15 of its 28 tiles (557 MiB
//! with the allocator of the standard library). Memory that the system gave never goes back to it: WebAssembly memory
//! never gets smaller.

use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::ptr::null_mut;
use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};

const STEP: usize = 16;
const CLASSES: usize = 16; // 16, 32, ... 256 bytes
const REFILL: usize = 64;
/// The most free blocks of a size that a thread keeps.
const KEEP: usize = 4 * REFILL;

thread_local! {
    static FREE: [Cell<*mut u8>; CLASSES] = const { [const { Cell::new(null_mut()) }; CLASSES] };
    static COUNT: [Cell<usize>; CLASSES] = const { [const { Cell::new(0) }; CLASSES] };
}

/// The shared pool of each size: a list of batches of `REFILL` free blocks. In a batch, the first word of each block is
/// the next block of the batch (null after the last); the second word of the first block is the next batch.
static POOL: [AtomicPtr<u8>; CLASSES] = [const { AtomicPtr::new(null_mut()) }; CLASSES];
static LOCK: [AtomicBool; CLASSES] = [const { AtomicBool::new(false) }; CLASSES];

/// Run `f` with the lock of the pool of class `c` (a spin lock: it holds only a few reads and writes).
fn with_pool<T>(c: usize, f: impl FnOnce(&AtomicPtr<u8>) -> T) -> T {
    while LOCK[c].compare_exchange_weak(false, true, Ordering::Acquire, Ordering::Relaxed).is_err() {
        std::hint::spin_loop();
    }
    let r = f(&POOL[c]);
    LOCK[c].store(false, Ordering::Release);
    r
}

unsafe fn next(b: *mut u8) -> *mut u8 {
    unsafe { (b as *mut *mut u8).read() }
}

unsafe fn set_next(b: *mut u8, n: *mut u8) {
    unsafe { (b as *mut *mut u8).write(n) }
}

/// The second word of the first block of a batch: the next batch of the pool.
unsafe fn batch_link(b: *mut u8) -> *mut *mut u8 {
    unsafe { (b as *mut *mut u8).add(1) }
}

pub struct Cached;

fn class(l: &Layout) -> Option<usize> {
    (l.size() <= STEP * CLASSES && l.align() <= STEP).then(|| l.size().max(1).div_ceil(STEP) - 1)
}

unsafe impl GlobalAlloc for Cached {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let Some(c) = class(&l) else {
            return unsafe { System.alloc(l) };
        };
        FREE.with(|f| {
            COUNT.with(|n| {
                let mut p = f[c].get();
                if p.is_null() {
                    // a batch of the pool, else a chunk of REFILL new blocks, linked into the free list
                    p = with_pool(c, |pool| {
                        let b = pool.load(Ordering::Relaxed);
                        if !b.is_null() {
                            pool.store(unsafe { batch_link(b).read() }, Ordering::Relaxed);
                        }
                        b
                    });
                    if p.is_null() {
                        let size = (c + 1) * STEP;
                        let chunk = unsafe { System.alloc(Layout::from_size_align_unchecked(size * REFILL, STEP)) };
                        if chunk.is_null() {
                            return null_mut();
                        }
                        for i in 0..REFILL {
                            let b = unsafe { chunk.add(i * size) };
                            let after = if i + 1 < REFILL { unsafe { b.add(size) } } else { null_mut() };
                            unsafe { set_next(b, after) };
                        }
                        p = chunk;
                    }
                    n[c].set(REFILL);
                }
                f[c].set(unsafe { next(p) });
                n[c].set(n[c].get().saturating_sub(1));
                p
            })
        })
    }

    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        let Some(c) = class(&l) else {
            return unsafe { System.dealloc(p, l) };
        };
        FREE.with(|f| {
            COUNT.with(|n| {
                unsafe { set_next(p, f[c].get()) };
                f[c].set(p);
                n[c].set(n[c].get() + 1);
                if n[c].get() >= KEEP + REFILL {
                    // the first REFILL blocks of the list go to the pool as one batch
                    let first = f[c].get();
                    let mut last = first;
                    for _ in 1..REFILL {
                        last = unsafe { next(last) };
                    }
                    f[c].set(unsafe { next(last) });
                    unsafe { set_next(last, null_mut()) };
                    n[c].set(n[c].get() - REFILL);
                    with_pool(c, |pool| {
                        unsafe { batch_link(first).write(pool.load(Ordering::Relaxed)) };
                        pool.store(first, Ordering::Relaxed);
                    });
                }
            })
        })
    }

    unsafe fn realloc(&self, p: *mut u8, l: Layout, new_size: usize) -> *mut u8 {
        let new = unsafe { Layout::from_size_align_unchecked(new_size, l.align()) };
        if class(&l).is_none() && class(&new).is_none() {
            return unsafe { System.realloc(p, l, new_size) };
        }
        let q = unsafe { self.alloc(new) };
        if !q.is_null() {
            unsafe {
                std::ptr::copy_nonoverlapping(p, q, l.size().min(new_size));
                self.dealloc(p, l);
            }
        }
        q
    }
}

//! Native scanning core for Minecraft Region Fixer Modern.
//!
//! The Python scanner parses every chunk of every region file into a full NBT
//! tree, one region file per worker process. This crate does the same work
//! without building the tree and without leaving the process: region files are
//! memory mapped and spread over a rayon thread pool, so a scan uses every
//! core without paying for pickling results back to a parent process.
//!
//! Results are streamed back through a channel so the caller can keep a
//! progress bar moving while the scan runs.

pub mod mutf8;
pub mod nbt;
pub mod region;
pub mod scan;

#[cfg(feature = "python")]
mod python {

    use super::scan;

    use std::path::PathBuf;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::mpsc::{self, Receiver, TryRecvError};
    use std::sync::Arc;

    use pyo3::prelude::*;
    use pyo3::types::PyList;
    use rayon::prelude::*;

    /// One scanned region file, as handed back to Python.
    struct FileResult {
        path: String,
        status: i32,
        chunks: Vec<(u8, u8, Option<i64>, i32)>,
        fallback: Option<String>,
    }

    /// A running scan of a list of region files.
    ///
    /// `next_result` returns finished files as they arrive, in whatever order the
    /// thread pool completes them.
    #[pyclass(unsendable)]
    pub struct RegionScanner {
        receiver: Option<Receiver<FileResult>>,
        cancelled: Arc<AtomicBool>,
        remaining: Arc<AtomicUsize>,
        total: usize,
    }

    #[pymethods]
    impl RegionScanner {
        /// Start scanning `paths` on `threads` worker threads.
        ///
        /// `threads` of zero lets rayon pick one thread per logical core.
        #[new]
        #[pyo3(signature = (paths, entity_limit, threads = 0))]
        fn new(paths: Vec<String>, entity_limit: i64, threads: usize) -> PyResult<Self> {
            let total = paths.len();
            let (sender, receiver) = mpsc::channel();
            let cancelled = Arc::new(AtomicBool::new(false));
            let remaining = Arc::new(AtomicUsize::new(total));

            let pool = rayon::ThreadPoolBuilder::new()
                .num_threads(threads)
                .thread_name(|i| format!("regionfixer-{i}"))
                .build()
                .map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!(
                        "could not start the native thread pool: {e}"
                    ))
                })?;

            let worker_cancelled = Arc::clone(&cancelled);
            // The pool owns its own thread, so building the scan does not block
            // the interpreter and Python keeps handling Ctrl-C.
            std::thread::spawn(move || {
                pool.install(|| {
                    paths.into_par_iter().for_each_with(sender, |sender, path| {
                        if worker_cancelled.load(Ordering::Relaxed) {
                            return;
                        }
                        let buf = PathBuf::from(&path);
                        let scanned = scan::scan_region_file(&buf, entity_limit);
                        let _ = sender.send(FileResult {
                            path,
                            status: scanned.status,
                            chunks: scanned.chunks,
                            fallback: scanned.fallback,
                        });
                    });
                });
            });

            Ok(RegionScanner {
                receiver: Some(receiver),
                cancelled,
                remaining,
                total,
            })
        }

        /// Return the next finished file, or `None` when none is ready yet.
        ///
        /// The tuple is `(path, region_status, chunks, fallback_reason)`, where
        /// `chunks` is a list of `(x, z, num_entities, chunk_status)`.
        fn next_result(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
            let receiver = match self.receiver.as_ref() {
                Some(receiver) => receiver,
                None => return Ok(None),
            };
            match receiver.try_recv() {
                Ok(result) => {
                    self.remaining.fetch_sub(1, Ordering::Relaxed);
                    Ok(Some(result.to_py(py)?))
                }
                Err(TryRecvError::Empty) => Ok(None),
                Err(TryRecvError::Disconnected) => {
                    self.receiver = None;
                    self.remaining.store(0, Ordering::Relaxed);
                    Ok(None)
                }
            }
        }

        /// True once every file has been handed to the caller.
        #[getter]
        fn finished(&self) -> bool {
            self.receiver.is_none() || self.remaining.load(Ordering::Relaxed) == 0
        }

        /// Number of files that have not been returned yet.
        #[getter]
        fn remaining(&self) -> usize {
            self.remaining.load(Ordering::Relaxed)
        }

        #[getter]
        fn total(&self) -> usize {
            self.total
        }

        /// Ask the worker threads to stop handing out new files.
        fn cancel(&mut self) {
            self.cancelled.store(true, Ordering::Relaxed);
            self.receiver = None;
        }
    }

    impl FileResult {
        fn to_py(self, py: Python<'_>) -> PyResult<Py<PyAny>> {
            let chunks = PyList::empty(py);
            for (x, z, num_entities, status) in self.chunks {
                chunks.append((x, z, num_entities, status))?;
            }
            let tuple = (self.path, self.status, chunks, self.fallback).into_pyobject(py)?;
            Ok(tuple.into_any().unbind())
        }
    }

    /// Scan every region file and return the results in one list.
    ///
    /// Useful for tests and benchmarks; the interactive scanners use
    /// [`RegionScanner`] so they can report progress.
    #[pyfunction]
    #[pyo3(signature = (paths, entity_limit, threads = 0))]
    fn scan_region_files(
        py: Python<'_>,
        paths: Vec<String>,
        entity_limit: i64,
        threads: usize,
    ) -> PyResult<Py<PyAny>> {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .build()
            .map_err(|e| {
                pyo3::exceptions::PyRuntimeError::new_err(format!(
                    "could not start the native thread pool: {e}"
                ))
            })?;

        let scanned: Vec<FileResult> = py.detach(|| {
            pool.install(|| {
                paths
                    .into_par_iter()
                    .map(|path| {
                        let buf = PathBuf::from(&path);
                        let scanned = scan::scan_region_file(&buf, entity_limit);
                        FileResult {
                            path,
                            status: scanned.status,
                            chunks: scanned.chunks,
                            fallback: scanned.fallback,
                        }
                    })
                    .collect()
            })
        });

        let out = PyList::empty(py);
        for result in scanned {
            out.append(result.to_py(py)?)?;
        }
        Ok(out.into_any().unbind())
    }

    /// Number of logical cores rayon would use by default.
    #[pyfunction]
    fn available_threads() -> usize {
        std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1)
    }

    #[pymodule]
    fn regionfixer_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_class::<RegionScanner>()?;
        m.add_function(wrap_pyfunction!(scan_region_files, m)?)?;
        m.add_function(wrap_pyfunction!(available_threads, m)?)?;
        m.add("__version__", env!("CARGO_PKG_VERSION"))?;
        Ok(())
    }
} // mod python

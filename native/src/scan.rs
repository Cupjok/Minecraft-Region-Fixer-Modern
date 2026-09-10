//! Chunk classification, mirroring `regionfixer_core/scan.py::scan_chunk`.

use std::fs::OpenOptions;
use std::path::Path;

use libdeflater::Decompressor;
use memmap2::Mmap;

use crate::nbt::{self, ChunkSkim, SkimError, TagLen};
use crate::region::{self, BlockError, RegionHeader};

// Chunk statuses, kept identical to regionfixer_core/constants.py.
pub const CHUNK_NOT_CREATED: i32 = -1;
pub const CHUNK_OK: i32 = 0;
pub const CHUNK_CORRUPTED: i32 = 1;
pub const CHUNK_WRONG_LOCATED: i32 = 2;
pub const CHUNK_TOO_MANY_ENTITIES: i32 = 3;
pub const CHUNK_SHARED_OFFSET: i32 = 4;
pub const CHUNK_MISSING_ENTITIES_TAG: i32 = 5;

// Region statuses.
pub const REGION_OK: i32 = 100;
pub const REGION_TOO_SMALL: i32 = 101;
pub const REGION_UNREADABLE: i32 = 102;
pub const REGION_UNREADABLE_PERMISSION_ERROR: i32 = 103;

/// Largest decompressed chunk the native scanner will handle on its own.
const MAX_DECOMPRESSED: usize = 256 * 1024 * 1024;

/// Result of scanning one region file.
pub struct RegionScan {
    pub status: i32,
    /// `(x, z, num_entities, chunk_status)` for every chunk that exists.
    pub chunks: Vec<(u8, u8, Option<i64>, i32)>,
    /// When set, the file holds something the native scanner does not model
    /// and the caller must rescan it with the Python scanner.
    pub fallback: Option<String>,
}

impl RegionScan {
    fn fallback(reason: impl Into<String>) -> Self {
        RegionScan {
            status: REGION_OK,
            chunks: Vec::new(),
            fallback: Some(reason.into()),
        }
    }
}

/// Scan a single region file.
pub fn scan_region_file(path: &Path, entity_limit: i64) -> RegionScan {
    let filename = match path.file_name().and_then(|n| n.to_str()) {
        Some(name) => name,
        None => return RegionScan::fallback("region file name is not valid unicode"),
    };
    let (region_x, region_z) = match region::region_coords(filename) {
        Some(coords) => coords,
        None => return RegionScan::fallback("region file name has no coordinates"),
    };

    // Region Fixer opens region files read-write, so a read-only file has to
    // be reported as a permission problem rather than scanned.
    let file = match OpenOptions::new().read(true).write(true).open(path) {
        Ok(file) => file,
        Err(err) => {
            let status = match err.kind() {
                std::io::ErrorKind::PermissionDenied => REGION_UNREADABLE_PERMISSION_ERROR,
                _ => REGION_UNREADABLE,
            };
            return RegionScan {
                status,
                chunks: Vec::new(),
                fallback: None,
            };
        }
    };

    let mmap = match unsafe { Mmap::map(&file) } {
        Ok(mmap) => mmap,
        Err(_) => return RegionScan::fallback("region file could not be mapped"),
    };
    let data: &[u8] = &mmap;

    let header = match region::parse(data) {
        Ok(header) => header,
        Err(_) => {
            return RegionScan {
                status: REGION_TOO_SMALL,
                chunks: Vec::new(),
                fallback: None,
            }
        }
    };

    let mut chunks = Vec::with_capacity(1024);
    let mut decompressor = Decompressor::new();
    let mut buffer: Vec<u8> = Vec::new();

    for x in 0..32usize {
        for z in 0..32usize {
            let global = (region_x * 32 + x as i64, region_z * 32 + z as i64);
            let outcome = scan_chunk(
                &header,
                data,
                path,
                (region_x, region_z),
                x,
                z,
                global,
                entity_limit,
                &mut decompressor,
                &mut buffer,
            );
            match outcome {
                Outcome::Fallback(reason) => return RegionScan::fallback(reason),
                Outcome::NotCreated => {}
                Outcome::Chunk(num_entities, status) => {
                    chunks.push((x as u8, z as u8, num_entities, status))
                }
            }
        }
    }

    // A wrong located chunk that also overlaps another chunk is really a
    // shared offset chunk. See the note in scan.py.
    for entry in chunks.iter_mut() {
        let m = &header.metadata[region::index(entry.0 as usize, entry.1 as usize)];
        if m.status == region::STATUS_CHUNK_OVERLAPPING && entry.3 == CHUNK_WRONG_LOCATED {
            entry.3 = CHUNK_SHARED_OFFSET;
        }
    }

    RegionScan {
        status: REGION_OK,
        chunks,
        fallback: None,
    }
}

enum Outcome {
    NotCreated,
    Chunk(Option<i64>, i32),
    Fallback(String),
}

#[allow(clippy::too_many_arguments)]
fn scan_chunk(
    header: &RegionHeader,
    data: &[u8],
    path: &Path,
    region_coords: (i64, i64),
    x: usize,
    z: usize,
    global: (i64, i64),
    entity_limit: i64,
    decompressor: &mut Decompressor,
    buffer: &mut Vec<u8>,
) -> Outcome {
    let (raw, compression) = match region::block_payload(header, data, x, z) {
        Ok(payload) => payload,
        Err(BlockError::Inconceived) => return Outcome::NotCreated,
        Err(_) => return Outcome::Chunk(None, CHUNK_CORRUPTED),
    };

    let external_storage;
    let raw = if header.metadata[region::index(x, z)].external {
        let external_path =
            region::external_chunk_path(path, region_coords.0, region_coords.1, x, z);
        match std::fs::read(&external_path) {
            Ok(bytes) => {
                external_storage = bytes;
                external_storage.as_slice()
            }
            // A missing payload is a ChunkDataError, which is a corrupted chunk.
            Err(_) => return Outcome::Chunk(None, CHUNK_CORRUPTED),
        }
    } else {
        raw
    };

    let payload = match decompress(raw, compression, decompressor, buffer) {
        Ok(payload) => payload,
        Err(Decompress::Unsupported) => {
            return Outcome::Fallback(format!(
                "chunk {},{} uses compression {} which the native scanner does not read",
                x, z, compression
            ))
        }
        Err(Decompress::Failed) => {
            return Outcome::Chunk(None, CHUNK_CORRUPTED);
        }
    };

    let skim = match nbt::skim(payload) {
        Ok(skim) => skim,
        // Every parse failure ends up as a corrupted chunk in Region Fixer.
        Err(SkimError::Truncated)
        | Err(SkimError::BadString)
        | Err(SkimError::UnknownTag)
        | Err(SkimError::TooDeep) => return Outcome::Chunk(None, CHUNK_CORRUPTED),
    };

    if skim.odd {
        return Outcome::Fallback(format!(
            "chunk {},{} has tags the native scanner does not model",
            x, z
        ));
    }

    classify(&skim, global, entity_limit, x, z)
}

/// The three region file families Region Fixer knows about.
enum ChunkKind {
    Level,
    Poi,
    Entities,
}

fn chunk_kind(skim: &ChunkSkim) -> Option<ChunkKind> {
    let dv = skim.data_version.unwrap_or(0);

    if dv < 2844 && skim.has_level {
        return Some(ChunkKind::Level);
    }
    if dv >= 2844 && (skim.has_structures || skim.has_sections_lower) {
        return Some(ChunkKind::Level);
    }
    if dv >= 1901 && skim.has_sections_upper {
        return Some(ChunkKind::Poi);
    }
    if dv >= 2681 && skim.has_entities_upper {
        return Some(ChunkKind::Entities);
    }
    None
}

fn classify(
    skim: &ChunkSkim,
    global: (i64, i64),
    entity_limit: i64,
    x: usize,
    z: usize,
) -> Outcome {
    let kind = match chunk_kind(skim) {
        Some(kind) => kind,
        // Python raises AssertionError here, which aborts the whole scan.
        None => return Outcome::Fallback(format!("chunk {},{} has an unrecognised type", x, z)),
    };

    match kind {
        ChunkKind::Poi => Outcome::Chunk(None, CHUNK_OK),

        ChunkKind::Entities => {
            let coords = match (skim.position_len, skim.position_xz) {
                (Some(2), Some(coords)) => coords,
                // A missing or oddly shaped Position raises out of scan_chunk.
                _ => {
                    return Outcome::Fallback(format!(
                        "entities chunk {},{} has no usable Position tag",
                        x, z
                    ))
                }
            };
            let num_entities = match skim.entities_upper_len {
                Some(TagLen::Len(len)) => len,
                _ => {
                    return Outcome::Fallback(format!(
                        "entities chunk {},{} has an Entities tag without a length",
                        x, z
                    ))
                }
            };
            let status = if coords != global {
                CHUNK_WRONG_LOCATED
            } else if num_entities > entity_limit {
                CHUNK_TOO_MANY_ENTITIES
            } else {
                CHUNK_OK
            };
            Outcome::Chunk(Some(num_entities), status)
        }

        ChunkKind::Level => {
            let has_data_version = skim.data_version.is_some();
            let dv = skim.data_version.unwrap_or(0);
            let modern = has_data_version && dv >= 2844;

            // get_chunk_data_coords
            let coords = if modern {
                match (skim.root_x, skim.root_z) {
                    (Some(cx), Some(cz)) => (cx, cz),
                    // A missing xPos or zPos is a KeyError.
                    _ => return Outcome::Chunk(None, CHUNK_MISSING_ENTITIES_TAG),
                }
            } else {
                if !skim.has_level {
                    return Outcome::Chunk(None, CHUNK_MISSING_ENTITIES_TAG);
                }
                match (skim.level_x, skim.level_z) {
                    (Some(cx), Some(cz)) => (cx, cz),
                    _ => return Outcome::Chunk(None, CHUNK_MISSING_ENTITIES_TAG),
                }
            };

            // Entity counting. Since 20w45a entities may live in their own
            // region files, so the count can legitimately be absent.
            let num_entities: Option<i64> = if has_data_version && dv >= 2681 {
                if dv >= 2844 {
                    match skim.entities_lower_len {
                        None => None,
                        Some(TagLen::Len(len)) => Some(len),
                        Some(TagLen::Unsupported) => {
                            return Outcome::Fallback(format!(
                                "chunk {},{} has an entities tag without a length",
                                x, z
                            ))
                        }
                    }
                } else {
                    if !skim.has_level {
                        return Outcome::Chunk(None, CHUNK_MISSING_ENTITIES_TAG);
                    }
                    match skim.level_entities_len {
                        None => None,
                        Some(TagLen::Len(len)) => Some(len),
                        Some(TagLen::Unsupported) => {
                            return Outcome::Fallback(format!(
                                "chunk {},{} has an Entities tag without a length",
                                x, z
                            ))
                        }
                    }
                }
            } else {
                if !skim.has_level {
                    return Outcome::Chunk(None, CHUNK_MISSING_ENTITIES_TAG);
                }
                match skim.level_entities_len {
                    // The tag is mandatory before 20w45a.
                    None => return Outcome::Chunk(None, CHUNK_MISSING_ENTITIES_TAG),
                    Some(TagLen::Len(len)) => Some(len),
                    Some(TagLen::Unsupported) => {
                        return Outcome::Fallback(format!(
                            "chunk {},{} has an Entities tag without a length",
                            x, z
                        ))
                    }
                }
            };

            let status = if coords != global {
                CHUNK_WRONG_LOCATED
            } else if num_entities.is_some_and(|n| n > entity_limit) {
                CHUNK_TOO_MANY_ENTITIES
            } else {
                CHUNK_OK
            };
            Outcome::Chunk(num_entities, status)
        }
    }
}

enum Decompress {
    /// A format the native scanner deliberately leaves to Python.
    Unsupported,
    /// Garbled data.
    Failed,
}

fn decompress<'a>(
    raw: &'a [u8],
    compression: u8,
    decompressor: &mut Decompressor,
    buffer: &'a mut Vec<u8>,
) -> Result<&'a [u8], Decompress> {
    match compression {
        region::COMPRESSION_NONE | region::COMPRESSION_NONE_LEGACY => Ok(raw),
        region::COMPRESSION_ZLIB | region::COMPRESSION_GZIP => {
            let gzip = compression == region::COMPRESSION_GZIP;
            let written = inflate(raw, gzip, decompressor, buffer)?;
            Ok(&buffer[..written])
        }
        // lz4-java framing is rare and only implemented in Python.
        region::COMPRESSION_LZ4 => Err(Decompress::Unsupported),
        _ => Err(Decompress::Failed),
    }
}

/// Inflate into `buffer`, growing it until the output fits.
fn inflate(
    raw: &[u8],
    gzip: bool,
    decompressor: &mut Decompressor,
    buffer: &mut Vec<u8>,
) -> Result<usize, Decompress> {
    // Chunks compress to roughly a tenth of their size; start generously so
    // the common case needs a single pass.
    let mut capacity = (raw.len() * 12).clamp(64 * 1024, MAX_DECOMPRESSED);

    loop {
        if buffer.len() < capacity {
            buffer.resize(capacity, 0);
        }
        let result = if gzip {
            decompressor.gzip_decompress(raw, &mut buffer[..capacity])
        } else {
            decompressor.zlib_decompress(raw, &mut buffer[..capacity])
        };
        match result {
            Ok(written) => return Ok(written),
            Err(libdeflater::DecompressionError::InsufficientSpace) => {
                if capacity >= MAX_DECOMPRESSED {
                    return Err(Decompress::Failed);
                }
                capacity = (capacity * 4).min(MAX_DECOMPRESSED);
            }
            Err(_) => return Err(Decompress::Failed),
        }
    }
}

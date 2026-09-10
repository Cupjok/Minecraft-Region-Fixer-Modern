//! Region file header parsing, mirroring `nbt/region.py`.

use std::path::{Path, PathBuf};

pub const SECTOR_LENGTH: usize = 4096;
pub const EXTERNAL_CHUNK_FLAG: u8 = 0x80;

pub const STATUS_CHUNK_OVERLAPPING: i32 = -5;
pub const STATUS_CHUNK_MISMATCHED_LENGTHS: i32 = -4;
pub const STATUS_CHUNK_ZERO_LENGTH: i32 = -3;
pub const STATUS_CHUNK_IN_HEADER: i32 = -2;
pub const STATUS_CHUNK_OUT_OF_FILE: i32 = -1;
pub const STATUS_CHUNK_OK: i32 = 0;
pub const STATUS_CHUNK_NOT_CREATED: i32 = 1;

pub const COMPRESSION_NONE_LEGACY: u8 = 0;
pub const COMPRESSION_GZIP: u8 = 1;
pub const COMPRESSION_ZLIB: u8 = 2;
pub const COMPRESSION_NONE: u8 = 3;
pub const COMPRESSION_LZ4: u8 = 4;

#[derive(Debug, Clone, Copy)]
pub struct ChunkMetadata {
    pub blockstart: u32,
    pub blocklength: u8,
    pub length: u32,
    pub compression: Option<u8>,
    pub external: bool,
    pub status: i32,
}

impl Default for ChunkMetadata {
    fn default() -> Self {
        ChunkMetadata {
            blockstart: 0,
            blocklength: 0,
            length: 0,
            compression: None,
            external: false,
            status: STATUS_CHUNK_NOT_CREATED,
        }
    }
}

impl ChunkMetadata {
    fn required_blocks(&self) -> usize {
        (self.length as usize + 3 + SECTOR_LENGTH) / SECTOR_LENGTH
    }

    fn is_created(&self) -> bool {
        self.blockstart != 0
    }
}

/// The region file is smaller than one header but not empty.
#[derive(Debug)]
pub struct NoRegionHeader;

/// Metadata for all 1024 chunk slots of one region file, indexed `x * 32 + z`.
pub struct RegionHeader {
    pub metadata: Vec<ChunkMetadata>,
    pub size: usize,
}

pub fn index(x: usize, z: usize) -> usize {
    x * 32 + z
}

/// Parse both header sectors and the per-chunk 5 byte headers.
pub fn parse(data: &[u8]) -> Result<RegionHeader, NoRegionHeader> {
    let size = data.len();
    let mut metadata = vec![ChunkMetadata::default(); 1024];

    if size == 0 {
        // Minecraft treats zero byte region files as empty, and so does
        // Region Fixer.
        return Ok(RegionHeader { metadata, size });
    }
    if size < 2 * SECTOR_LENGTH {
        return Err(NoRegionHeader);
    }

    for i in (0..SECTOR_LENGTH).step_by(4) {
        let x = (i / 4) % 32;
        let z = (i / 4) / 32;
        let m = &mut metadata[index(x, z)];

        let offset = u32::from_be_bytes([0, data[i], data[i + 1], data[i + 2]]);
        let length = data[i + 3];
        m.blockstart = offset;
        m.blocklength = length;

        m.status = if offset == 0 && length == 0 {
            STATUS_CHUNK_NOT_CREATED
        } else if length == 0 {
            STATUS_CHUNK_ZERO_LENGTH
        } else if offset < 2 {
            STATUS_CHUNK_IN_HEADER
        } else if SECTOR_LENGTH * offset as usize + 5 > size {
            STATUS_CHUNK_OUT_OF_FILE
        } else {
            STATUS_CHUNK_OK
        };
    }

    mark_overlapping(&mut metadata, size);
    parse_chunk_headers(&mut metadata, data, size);

    Ok(RegionHeader { metadata, size })
}

/// Flag chunks that claim sectors already claimed by another chunk.
///
/// This runs before the chunk headers are read, exactly as in the Python
/// version, so every chunk still reports `length == 0` and therefore needs a
/// single sector at minimum.
fn mark_overlapping(metadata: &mut [ChunkMetadata], size: usize) {
    let sector_count = size.div_ceil(SECTOR_LENGTH);
    if sector_count <= 2 {
        return;
    }

    // How many chunks claim each sector. Sectors 0 and 1 hold the header and
    // are never counted as overlapping.
    let mut claims = vec![0u16; sector_count];
    for m in metadata.iter() {
        if !m.is_created() {
            continue;
        }
        if m.blocklength == 0 || m.blockstart == 0 {
            continue;
        }
        let start = (m.blockstart as usize).max(2);
        let end = (m.blockstart as usize + (m.blocklength as usize).max(m.required_blocks()))
            .min(sector_count);
        for c in claims.iter_mut().take(end).skip(start) {
            *c += 1;
        }
    }

    for m in metadata.iter_mut() {
        if !m.is_created() || m.blocklength == 0 || m.blockstart == 0 {
            continue;
        }
        let start = (m.blockstart as usize).max(2);
        let end = (m.blockstart as usize + (m.blocklength as usize).max(m.required_blocks()))
            .min(sector_count);
        let shared = claims[start..end.max(start)].iter().any(|&c| c > 1);
        if shared
            && !matches!(
                m.status,
                STATUS_CHUNK_ZERO_LENGTH | STATUS_CHUNK_IN_HEADER | STATUS_CHUNK_OUT_OF_FILE
            )
        {
            m.status = STATUS_CHUNK_OVERLAPPING;
        }
    }
}

fn parse_chunk_headers(metadata: &mut [ChunkMetadata], data: &[u8], size: usize) {
    for m in metadata.iter_mut() {
        if !matches!(
            m.status,
            STATUS_CHUNK_OK | STATUS_CHUNK_OVERLAPPING | STATUS_CHUNK_MISMATCHED_LENGTHS
        ) {
            continue;
        }

        let start = m.blockstart as usize * SECTOR_LENGTH;
        if start + 5 > size {
            m.status = STATUS_CHUNK_OUT_OF_FILE;
            continue;
        }
        m.length = u32::from_be_bytes([
            data[start],
            data[start + 1],
            data[start + 2],
            data[start + 3],
        ]);
        let compression = data[start + 4];
        m.external = compression & EXTERNAL_CHUNK_FLAG != 0;
        m.compression = Some(compression & !EXTERNAL_CHUNK_FLAG);

        if start + m.length as usize + 4 > size {
            m.status = STATUS_CHUNK_OUT_OF_FILE;
        } else if m.length < 1 || (m.length == 1 && !m.external) {
            // External chunks legitimately carry only the compression byte.
            m.status = STATUS_CHUNK_ZERO_LENGTH;
        } else if m.length as usize + 4 > m.blocklength as usize * SECTOR_LENGTH {
            m.status = STATUS_CHUNK_MISMATCHED_LENGTHS;
        }
    }
}

/// The error kinds `RegionFile.get_blockdata` can raise.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlockError {
    /// The chunk was never generated.
    Inconceived,
    RegionHeader,
    ChunkHeader,
    ChunkData,
}

/// Locate the raw, still compressed payload of one chunk.
///
/// Returns the payload together with its compression id, or the error the
/// Python implementation would raise.
pub fn block_payload<'a>(
    header: &RegionHeader,
    data: &'a [u8],
    x: usize,
    z: usize,
) -> Result<(&'a [u8], u8), BlockError> {
    let m = &header.metadata[index(x, z)];
    let size = header.size;

    match m.status {
        STATUS_CHUNK_NOT_CREATED => return Err(BlockError::Inconceived),
        STATUS_CHUNK_IN_HEADER => return Err(BlockError::RegionHeader),
        STATUS_CHUNK_OUT_OF_FILE if m.length <= 1 || m.compression.is_none() => {
            return Err(BlockError::RegionHeader)
        }
        STATUS_CHUNK_ZERO_LENGTH => {
            return Err(if m.blocklength == 0 {
                BlockError::RegionHeader
            } else {
                BlockError::ChunkHeader
            })
        }
        _ => {}
    }
    if m.blockstart as usize * SECTOR_LENGTH + 5 >= size {
        return Err(BlockError::RegionHeader);
    }

    let compression = m.compression.ok_or(BlockError::ChunkData)?;
    let start = m.blockstart as usize * SECTOR_LENGTH + 5;
    let length = ((m.length as usize).saturating_sub(1)).min(size - start);
    Ok((&data[start..start + length], compression))
}

/// The error that a failed decompression or parse must be reported as.
pub fn payload_error(header: &RegionHeader, x: usize, z: usize) -> BlockError {
    match header.metadata[index(x, z)].status {
        STATUS_CHUNK_MISMATCHED_LENGTHS => BlockError::ChunkHeader,
        STATUS_CHUNK_OVERLAPPING => BlockError::ChunkHeader,
        _ => BlockError::ChunkData,
    }
}

/// Path of the `.mcc` file holding an oversized chunk.
pub fn external_chunk_path(
    region_path: &Path,
    region_x: i64,
    region_z: i64,
    x: usize,
    z: usize,
) -> PathBuf {
    let global_x = region_x * 32 + x as i64;
    let global_z = region_z * 32 + z as i64;
    let dir = region_path.parent().unwrap_or_else(|| Path::new(""));
    dir.join(format!("c.{}.{}.mcc", global_x, global_z))
}

/// Read the `X` and `Z` of `r.X.Z.mca` style names.
pub fn region_coords(filename: &str) -> Option<(i64, i64)> {
    let mut parts = filename.split('.');
    parts.next()?;
    let x = parts.next()?.parse::<i64>().ok()?;
    let z = parts.next()?.parse::<i64>().ok()?;
    Some((x, z))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_file_has_no_chunks() {
        let header = parse(&[]).unwrap();
        assert!(header
            .metadata
            .iter()
            .all(|m| m.status == STATUS_CHUNK_NOT_CREATED));
    }

    #[test]
    fn short_file_has_no_header() {
        assert!(parse(&[0u8; 100]).is_err());
    }

    #[test]
    fn parses_region_coords() {
        assert_eq!(region_coords("r.-1.5.mca"), Some((-1, 5)));
        assert_eq!(region_coords("nonsense"), None);
    }
}

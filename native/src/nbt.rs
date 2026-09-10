//! A read-only NBT skimmer.
//!
//! The Python implementation in `nbt/nbt.py` materialises the complete tag
//! tree, which means every block state array of every chunk is turned into a
//! Python list. Region Fixer only ever looks at a handful of root level tags,
//! so this module walks the same byte stream but allocates nothing: tags that
//! are not interesting are skipped by advancing the cursor.
//!
//! Where the Python parser raises, this module returns `Err(SkimError)`, and
//! where the Python parser would produce something Region Fixer cannot
//! interpret the skim is flagged as `odd` so the caller can fall back to the
//! Python scanner for that region file.

use crate::mutf8;

pub const T_END: u8 = 0;
pub const T_BYTE: u8 = 1;
pub const T_SHORT: u8 = 2;
pub const T_INT: u8 = 3;
pub const T_LONG: u8 = 4;
pub const T_FLOAT: u8 = 5;
pub const T_DOUBLE: u8 = 6;
pub const T_BYTE_ARRAY: u8 = 7;
pub const T_STRING: u8 = 8;
pub const T_LIST: u8 = 9;
pub const T_COMPOUND: u8 = 10;
pub const T_INT_ARRAY: u8 = 11;
pub const T_LONG_ARRAY: u8 = 12;

/// Maximum compound/list nesting accepted before the data is called corrupted.
const MAX_DEPTH: u32 = 512;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkimError {
    /// The stream ended in the middle of a tag.
    Truncated,
    /// A tag id that the Python parser does not know about.
    UnknownTag,
    /// A name or string that modified UTF-8 cannot decode.
    BadString,
    /// Nesting deeper than `MAX_DEPTH`.
    TooDeep,
}

struct Cursor<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(data: &'a [u8]) -> Self {
        Cursor { data, pos: 0 }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], SkimError> {
        let end = self.pos.checked_add(n).ok_or(SkimError::Truncated)?;
        if end > self.data.len() {
            return Err(SkimError::Truncated);
        }
        let out = &self.data[self.pos..end];
        self.pos = end;
        Ok(out)
    }

    fn skip(&mut self, n: usize) -> Result<(), SkimError> {
        self.take(n).map(|_| ())
    }

    fn u8(&mut self) -> Result<u8, SkimError> {
        Ok(self.take(1)?[0])
    }

    fn i16(&mut self) -> Result<i16, SkimError> {
        let b = self.take(2)?;
        Ok(i16::from_be_bytes([b[0], b[1]]))
    }

    fn i32(&mut self) -> Result<i32, SkimError> {
        let b = self.take(4)?;
        Ok(i32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }

    fn i64(&mut self) -> Result<i64, SkimError> {
        let b = self.take(8)?;
        Ok(i64::from_be_bytes([
            b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
        ]))
    }

    /// Read a length prefixed string and validate it the way Python does.
    fn string(&mut self) -> Result<&'a [u8], SkimError> {
        let len = self.i16()?;
        // TAG_Short is signed; a negative length makes `buffer.read()` return
        // fewer bytes than asked for, which raises StructError in Python.
        if len < 0 {
            return Err(SkimError::Truncated);
        }
        let raw = self.take(len as usize)?;
        if !mutf8::is_valid(raw) {
            return Err(SkimError::BadString);
        }
        Ok(raw)
    }
}

/// The number of elements `len()` would report for a tag, when that is defined.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TagLen {
    /// `len()` is defined and returns this value.
    Len(i64),
    /// `len()` is not defined, or is defined on something Region Fixer does
    /// not model (a string counts characters, not bytes).
    Unsupported,
}

/// Everything the scanner needs from a single chunk.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct ChunkSkim {
    pub data_version: Option<i64>,
    pub has_level: bool,
    pub level_is_compound: bool,
    pub has_structures: bool,
    pub has_sections_lower: bool,
    pub has_sections_upper: bool,
    pub has_entities_upper: bool,
    pub has_entities_lower: bool,
    pub entities_upper_len: Option<TagLen>,
    pub entities_lower_len: Option<TagLen>,
    pub root_x: Option<i64>,
    pub root_z: Option<i64>,
    pub position_xz: Option<(i64, i64)>,
    pub position_len: Option<usize>,
    pub level_has_entities: bool,
    pub level_entities_len: Option<TagLen>,
    pub level_x: Option<i64>,
    pub level_z: Option<i64>,
    pub level_has_x: bool,
    pub level_has_z: bool,
    /// Set when the chunk holds something this skimmer cannot reproduce
    /// faithfully. The caller must rescan the region file in Python.
    pub odd: bool,
}

/// Skim one decompressed chunk payload.
pub fn skim(data: &[u8]) -> Result<ChunkSkim, SkimError> {
    let mut c = Cursor::new(data);
    let mut out = ChunkSkim::default();

    // NBTFile.parse_file: the first record must be a compound tag.
    if c.u8()? != T_COMPOUND {
        return Err(SkimError::UnknownTag);
    }
    c.string()?; // root name

    read_root(&mut c, &mut out)?;
    Ok(out)
}

fn read_root(c: &mut Cursor, out: &mut ChunkSkim) -> Result<(), SkimError> {
    loop {
        let tag = c.u8()?;
        if tag == T_END {
            return Ok(());
        }
        let name = c.string()?;

        match name {
            b"DataVersion" => {
                let (value, unusual) = numeric(c, tag)?;
                out.odd |= unusual;
                set_once(&mut out.data_version, value, &mut out.odd);
            }
            b"Level" => {
                mark_once(&mut out.has_level, &mut out.odd);
                if tag == T_COMPOUND {
                    out.level_is_compound = true;
                    read_level(c, out)?;
                } else {
                    // `chunk['Level']['Entities']` raises TypeError on a
                    // non-compound. Let Python decide.
                    out.odd = true;
                    skip_payload(c, tag, 1)?;
                }
            }
            b"structures" => {
                out.has_structures = true;
                skip_payload(c, tag, 1)?;
            }
            b"sections" => {
                out.has_sections_lower = true;
                skip_payload(c, tag, 1)?;
            }
            b"Sections" => {
                out.has_sections_upper = true;
                skip_payload(c, tag, 1)?;
            }
            b"Entities" => {
                mark_once(&mut out.has_entities_upper, &mut out.odd);
                out.entities_upper_len = Some(sized_payload(c, tag, 1)?);
            }
            b"entities" => {
                mark_once(&mut out.has_entities_lower, &mut out.odd);
                out.entities_lower_len = Some(sized_payload(c, tag, 1)?);
            }
            b"xPos" => {
                let (value, unusual) = numeric(c, tag)?;
                out.odd |= unusual;
                set_once(&mut out.root_x, value, &mut out.odd);
            }
            b"zPos" => {
                let (value, unusual) = numeric(c, tag)?;
                out.odd |= unusual;
                set_once(&mut out.root_z, value, &mut out.odd);
            }
            b"Position" => {
                if out.position_len.is_some() {
                    out.odd = true;
                }
                read_position(c, tag, out)?;
            }
            _ => skip_payload(c, tag, 1)?,
        }
    }
}

fn read_level(c: &mut Cursor, out: &mut ChunkSkim) -> Result<(), SkimError> {
    loop {
        let tag = c.u8()?;
        if tag == T_END {
            return Ok(());
        }
        let name = c.string()?;

        match name {
            b"Entities" => {
                mark_once(&mut out.level_has_entities, &mut out.odd);
                out.level_entities_len = Some(sized_payload(c, tag, 2)?);
            }
            b"xPos" => {
                mark_once(&mut out.level_has_x, &mut out.odd);
                let (value, unusual) = numeric(c, tag)?;
                out.odd |= unusual;
                set_once(&mut out.level_x, value, &mut out.odd);
            }
            b"zPos" => {
                mark_once(&mut out.level_has_z, &mut out.odd);
                let (value, unusual) = numeric(c, tag)?;
                out.odd |= unusual;
                set_once(&mut out.level_z, value, &mut out.odd);
            }
            _ => skip_payload(c, tag, 2)?,
        }
    }
}

fn set_once(slot: &mut Option<i64>, value: Option<i64>, odd: &mut bool) {
    if slot.is_some() {
        *odd = true;
        return;
    }
    *slot = value;
}

fn mark_once(slot: &mut bool, odd: &mut bool) {
    if *slot {
        *odd = true;
    }
    *slot = true;
}

/// Read a tag that Region Fixer uses as an integer.
///
/// The second half of the return value marks a tag that is not an integer, in
/// which case the caller must hand the region file back to Python.
fn numeric(c: &mut Cursor, tag: u8) -> Result<(Option<i64>, bool), SkimError> {
    match tag {
        T_BYTE => Ok((Some(c.take(1)?[0] as i8 as i64), false)),
        T_SHORT => Ok((Some(c.i16()? as i64), false)),
        T_INT => Ok((Some(c.i32()? as i64), false)),
        T_LONG => Ok((Some(c.i64()?), false)),
        _ => {
            // Floats compare unequal to the integer coordinates in ways that
            // depend on Python semantics, and anything else is not a number
            // at all.
            skip_payload(c, tag, 1)?;
            Ok((None, true))
        }
    }
}

/// Read `Position`, the two element int array used by entities chunks.
fn read_position(c: &mut Cursor, tag: u8, out: &mut ChunkSkim) -> Result<(), SkimError> {
    if tag != T_INT_ARRAY {
        out.odd = true;
        return skip_payload(c, tag, 1);
    }
    let len = c.i32()?;
    if len < 0 {
        // Struct(">-1i") raises in Python.
        return Err(SkimError::Truncated);
    }
    let len = len as usize;
    let raw = c.take(len.checked_mul(4).ok_or(SkimError::Truncated)?)?;
    out.position_len = Some(len);
    if len == 2 {
        let x = i32::from_be_bytes([raw[0], raw[1], raw[2], raw[3]]) as i64;
        let z = i32::from_be_bytes([raw[4], raw[5], raw[6], raw[7]]) as i64;
        out.position_xz = Some((x, z));
    }
    Ok(())
}

/// Skip a payload while recording what `len()` would return for it.
fn sized_payload(c: &mut Cursor, tag: u8, depth: u32) -> Result<TagLen, SkimError> {
    match tag {
        T_LIST => {
            let element = c.u8()?;
            let len = c.i32()?;
            skip_list_elements(c, element, len, depth)?;
            Ok(TagLen::Len(len.max(0) as i64))
        }
        T_BYTE_ARRAY => {
            let len = read_array_len(c, 1)?;
            Ok(TagLen::Len(len))
        }
        T_INT_ARRAY => {
            let len = read_array_len(c, 4)?;
            Ok(TagLen::Len(len))
        }
        T_LONG_ARRAY => {
            let len = read_array_len(c, 8)?;
            Ok(TagLen::Len(len))
        }
        T_COMPOUND => {
            let len = skip_compound_counting(c, depth)?;
            Ok(TagLen::Len(len))
        }
        _ => {
            skip_payload(c, tag, depth)?;
            Ok(TagLen::Unsupported)
        }
    }
}

fn read_array_len(c: &mut Cursor, element_size: usize) -> Result<i64, SkimError> {
    let len = c.i32()?;
    if len < 0 {
        // Python builds a struct format string with a negative repeat count,
        // which raises struct.error and becomes a corrupted chunk.
        return Err(SkimError::Truncated);
    }
    let len = len as usize;
    c.skip(len.checked_mul(element_size).ok_or(SkimError::Truncated)?)?;
    Ok(len as i64)
}

fn skip_list_elements(c: &mut Cursor, element: u8, len: i32, depth: u32) -> Result<(), SkimError> {
    if len <= 0 {
        // `for x in range(negative)` runs zero times and never looks at the
        // element type, so an unknown element id is not an error here.
        return Ok(());
    }
    if depth > MAX_DEPTH {
        return Err(SkimError::TooDeep);
    }
    // Fixed width elements can be skipped in one step.
    let width = match element {
        T_BYTE => Some(1usize),
        T_SHORT => Some(2),
        T_INT => Some(4),
        T_LONG => Some(8),
        T_FLOAT => Some(4),
        T_DOUBLE => Some(8),
        _ => None,
    };
    if let Some(width) = width {
        let total = (len as usize)
            .checked_mul(width)
            .ok_or(SkimError::Truncated)?;
        return c.skip(total);
    }
    for _ in 0..len {
        skip_payload(c, element, depth + 1)?;
    }
    Ok(())
}

fn skip_compound_counting(c: &mut Cursor, depth: u32) -> Result<i64, SkimError> {
    if depth > MAX_DEPTH {
        return Err(SkimError::TooDeep);
    }
    let mut count = 0i64;
    loop {
        let tag = c.u8()?;
        if tag == T_END {
            return Ok(count);
        }
        c.string()?;
        skip_payload(c, tag, depth + 1)?;
        count += 1;
    }
}

fn skip_payload(c: &mut Cursor, tag: u8, depth: u32) -> Result<(), SkimError> {
    match tag {
        T_END => {
            // _TAG_End insists the byte it reads is zero.
            if c.u8()? != 0 {
                Err(SkimError::UnknownTag)
            } else {
                Ok(())
            }
        }
        T_BYTE => c.skip(1),
        T_SHORT => c.skip(2),
        T_INT | T_FLOAT => c.skip(4),
        T_LONG | T_DOUBLE => c.skip(8),
        T_BYTE_ARRAY => read_array_len(c, 1).map(|_| ()),
        T_INT_ARRAY => read_array_len(c, 4).map(|_| ()),
        T_LONG_ARRAY => read_array_len(c, 8).map(|_| ()),
        T_STRING => c.string().map(|_| ()),
        T_LIST => {
            let element = c.u8()?;
            let len = c.i32()?;
            if len > 0 && !is_known_tag(element) {
                // TAGLIST[element] raises KeyError inside TAG_List.
                return Err(SkimError::UnknownTag);
            }
            skip_list_elements(c, element, len, depth + 1)
        }
        T_COMPOUND => skip_compound_counting(c, depth).map(|_| ()),
        _ => Err(SkimError::UnknownTag),
    }
}

fn is_known_tag(tag: u8) -> bool {
    tag <= T_LONG_ARRAY
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build `TAG_Compound("") { DataVersion: 3465, xPos: 1, zPos: -2 }`.
    fn sample() -> Vec<u8> {
        let mut v = vec![T_COMPOUND, 0, 0];
        for (name, value) in [("DataVersion", 3465i32), ("xPos", 1), ("zPos", -2)] {
            v.push(T_INT);
            v.extend_from_slice(&(name.len() as i16).to_be_bytes());
            v.extend_from_slice(name.as_bytes());
            v.extend_from_slice(&value.to_be_bytes());
        }
        v.push(T_END);
        v
    }

    #[test]
    fn reads_root_scalars() {
        let s = skim(&sample()).unwrap();
        assert_eq!(s.data_version, Some(3465));
        assert_eq!(s.root_x, Some(1));
        assert_eq!(s.root_z, Some(-2));
        assert!(!s.odd);
    }

    #[test]
    fn truncation_is_an_error() {
        let full = sample();
        assert_eq!(skim(&full[..full.len() - 3]), Err(SkimError::Truncated));
    }

    #[test]
    fn rejects_non_compound_root() {
        assert_eq!(skim(&[T_INT, 0, 0]), Err(SkimError::UnknownTag));
    }

    #[test]
    fn negative_list_length_is_empty() {
        let mut v = vec![T_COMPOUND, 0, 0, T_LIST];
        v.extend_from_slice(&8i16.to_be_bytes());
        v.extend_from_slice(b"Entities");
        v.push(T_COMPOUND);
        v.extend_from_slice(&(-1i32).to_be_bytes());
        v.push(T_END);
        let s = skim(&v).unwrap();
        assert_eq!(s.entities_upper_len, Some(TagLen::Len(0)));
    }
}

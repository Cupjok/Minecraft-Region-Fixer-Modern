//! Validation of Java modified UTF-8 strings.
//!
//! The rules implemented here mirror `mutf8/mutf8.py` exactly, so a name or
//! string that the Python decoder rejects is also rejected by the native
//! scanner. Region Fixer turns such a failure into a corrupted chunk.

/// Returns `true` when `s` decodes cleanly as modified UTF-8.
pub fn is_valid(s: &[u8]) -> bool {
    // Almost every tag name and string in a chunk is ASCII, and the standard
    // library check for that is vectorised. Note that plain UTF-8 is not a
    // usable fast path: a four byte sequence is valid UTF-8 but the Python
    // decoder rejects it.
    if s.is_ascii() && !s.contains(&0) {
        return true;
    }
    validate_slow(s)
}

fn validate_slow(s: &[u8]) -> bool {
    let len = s.len();
    let mut i = 0;

    while i < len {
        let b1 = s[i];
        i += 1;

        if b1 == 0 {
            // Embedded NULL byte, the Python decoder raises UnicodeDecodeError.
            return false;
        }
        if b1 < 0x80 {
            continue;
        }
        if b1 & 0xE0 == 0xC0 {
            if i >= len {
                return false;
            }
            i += 1;
        } else if b1 & 0xF0 == 0xE0 {
            if i + 1 >= len {
                return false;
            }
            let b2 = s[i];
            if b1 == 0xED && b2 & 0xF0 == 0xA0 {
                // Possible six byte codepoint, the Python decoder insists on
                // having the full sequence available before it falls back to
                // the three byte reading.
                if i + 4 >= len {
                    return false;
                }
                let b4 = s[i + 2];
                let b5 = s[i + 3];
                if b4 == 0xED && b5 & 0xF0 == 0xB0 {
                    i += 5;
                    continue;
                }
            }
            i += 2;
        } else {
            // The Python decoder raises RuntimeError here.
            return false;
        }
    }

    true
}

#[cfg(test)]
mod tests {
    use super::is_valid;

    #[test]
    fn accepts_ascii() {
        assert!(is_valid(b"Level"));
    }

    #[test]
    fn rejects_embedded_nul() {
        assert!(!is_valid(b"a\0b"));
    }

    #[test]
    fn accepts_encoded_nul() {
        assert!(is_valid(&[0xC0, 0x80]));
    }

    #[test]
    fn rejects_four_byte_utf8() {
        // Real UTF-8 four byte sequences are not valid modified UTF-8.
        assert!(!is_valid(&[0xF0, 0x9F, 0x92, 0xA9]));
    }

    #[test]
    fn accepts_six_byte_surrogate_pair() {
        assert!(is_valid(&[0xED, 0xA0, 0xBD, 0xED, 0xB2, 0xA9]));
    }

    #[test]
    fn rejects_truncated_two_byte() {
        assert!(!is_valid(&[0xC2]));
    }
}

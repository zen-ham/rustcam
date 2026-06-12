//! Pixel format conversions from BGRA8 source to the user-facing formats.
//!
//! No OpenCV. Pure scalar Rust that LLVM auto-vectorises well at `opt-level=3`.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fmt {
    Bgra,
    Bgr,
    Rgba,
    Rgb,
    Gray,
}

impl Fmt {
    pub fn parse(s: &str) -> Option<Self> {
        match s.to_ascii_lowercase().as_str() {
            "bgra" => Some(Self::Bgra),
            "bgr" => Some(Self::Bgr),
            "rgba" => Some(Self::Rgba),
            "rgb" => Some(Self::Rgb),
            "gray" | "grey" => Some(Self::Gray),
            _ => None,
        }
    }

    /// Channel count of the output.
    pub fn channels(self) -> usize {
        match self {
            Fmt::Bgra | Fmt::Rgba => 4,
            Fmt::Bgr | Fmt::Rgb => 3,
            Fmt::Gray => 1,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Fmt::Bgra => "bgra",
            Fmt::Bgr => "bgr",
            Fmt::Rgba => "rgba",
            Fmt::Rgb => "rgb",
            Fmt::Gray => "gray",
        }
    }
}

/// Convert a BGRA8 source buffer into `fmt`, writing into `dst`.
///
/// `src` is `(w * h * 4)` bytes, BGRA order (one frame, no padding).
/// `dst` must be sized to `w * h * fmt.channels()`.
pub fn bgra_to(fmt: Fmt, src: &[u8], dst: &mut [u8], w: usize, h: usize) {
    let pixels = w * h;
    debug_assert_eq!(src.len(), pixels * 4);
    debug_assert_eq!(dst.len(), pixels * fmt.channels());
    match fmt {
        Fmt::Bgra => dst.copy_from_slice(src),
        Fmt::Bgr => {
            for i in 0..pixels {
                let s = i * 4;
                let d = i * 3;
                dst[d] = src[s];
                dst[d + 1] = src[s + 1];
                dst[d + 2] = src[s + 2];
            }
        }
        Fmt::Rgba => {
            for i in 0..pixels {
                let s = i * 4;
                let d = i * 4;
                dst[d] = src[s + 2];
                dst[d + 1] = src[s + 1];
                dst[d + 2] = src[s];
                dst[d + 3] = src[s + 3];
            }
        }
        Fmt::Rgb => {
            for i in 0..pixels {
                let s = i * 4;
                let d = i * 3;
                dst[d] = src[s + 2];
                dst[d + 1] = src[s + 1];
                dst[d + 2] = src[s];
            }
        }
        Fmt::Gray => {
            // ITU-R BT.601 luma. Fast scalar form: (66*R + 129*G + 25*B + 128) >> 8 + 16
            // We use straight 0..255 range here (no studio swing), which matches numpy's
            // typical grayscale conventions.
            for i in 0..pixels {
                let s = i * 4;
                let b = src[s] as u32;
                let g = src[s + 1] as u32;
                let r = src[s + 2] as u32;
                let y = (77 * r + 150 * g + 29 * b + 128) >> 8;
                dst[i] = y as u8;
            }
        }
    }
}

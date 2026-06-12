use crate::errors::RustcamError;

#[derive(Clone, Copy, Debug)]
pub struct Region {
    pub left: u32,
    pub top: u32,
    pub right: u32,
    pub bottom: u32,
}

impl Region {
    pub fn full(w: u32, h: u32) -> Self {
        Self { left: 0, top: 0, right: w, bottom: h }
    }

    pub fn width(&self) -> u32 {
        self.right - self.left
    }

    pub fn height(&self) -> u32 {
        self.bottom - self.top
    }

    pub fn from_tuple(t: (i64, i64, i64, i64), out_w: u32, out_h: u32) -> Result<Self, RustcamError> {
        let (l, top, r, b) = t;
        if l < 0 || top < 0 || r < 0 || b < 0 {
            return Err(RustcamError::Value(format!(
                "region bounds must be >= 0, got {:?}",
                t
            )));
        }
        let l = l as u32;
        let top = top as u32;
        let r = r as u32;
        let b = b as u32;
        if r <= l || b <= top {
            return Err(RustcamError::Value(format!(
                "region must be non-empty (left<right, top<bottom), got ({},{},{},{})",
                l, top, r, b
            )));
        }
        if r > out_w || b > out_h {
            return Err(RustcamError::Value(format!(
                "region ({},{},{},{}) exceeds output bounds ({}x{})",
                l, top, r, b, out_w, out_h
            )));
        }
        Ok(Self { left: l, top, right: r, bottom: b })
    }

    pub fn as_tuple(&self) -> (u32, u32, u32, u32) {
        (self.left, self.top, self.right, self.bottom)
    }
}

/// Copy a region of a BGRA frame (mapped staging texture) into a tightly-packed dst buffer.
///
/// `src_pitch` is the mapped texture's RowPitch in BYTES (may exceed `src_width*4`).
/// `dst` must be pre-sized to `region.width() * region.height() * 4` bytes.
///
/// Safety: caller must ensure `src` points to at least `src_pitch * src_height` valid bytes.
pub unsafe fn crop_copy_bgra(
    dst: &mut [u8],
    src: *const u8,
    src_pitch: usize,
    region: Region,
) {
    let row_bytes = (region.width() as usize) * 4;
    let dst_pitch = row_bytes;
    for y in 0..region.height() as usize {
        let src_row = src
            .add((region.top as usize + y) * src_pitch + (region.left as usize) * 4);
        let dst_row = dst.as_mut_ptr().add(y * dst_pitch);
        std::ptr::copy_nonoverlapping(src_row, dst_row, row_bytes);
    }
}

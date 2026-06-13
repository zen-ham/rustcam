//! Manual cursor compositing using DDA's own pointer data.
//!
//! Replaces the GDI / `IDXGISurface1::GetDC` path that used to live here.
//! The GDI path forced a CPU↔GPU sync at GetDC; when DWM was awake
//! compositing windowed apps the sync queued behind DWM's per-vsync work
//! and capped throughput at 47-130 fps on real moving content. See
//! `CURSOR_GDI_HIDDEN_BOTTLENECK.md` in the zentape repo for the full
//! analysis.
//!
//! New strategy:
//!
//! 1. After `AcquireNextFrame`, copy `DXGI_OUTDUPL_FRAME_INFO::PointerPosition`
//!    into [`CursorState`]. DDA only fills it when `LastMouseUpdateTime != 0`,
//!    so the previous value sticks across frames where the cursor didn't move.
//! 2. If the frame info reports `PointerShapeBufferSize > 0`, call
//!    `dup.GetFramePointerShape()` to refresh the cached shape. Otherwise
//!    re-use the cache.
//! 3. After `Map(staging)` + crop into the user's destination buffer (BGRA),
//!    composite the cursor in software directly into that buffer with one
//!    of three blend modes:
//!    - `COLOR` (kind 2): straight-alpha BGRA, standard alpha blend
//!    - `MASKED_COLOR` (kind 4): per-pixel alpha selects copy vs XOR
//!    - `MONOCHROME` (kind 1): 1-bit AND mask + 1-bit XOR mask; this is
//!      the inverting-I-beam case
//!
//! No `GetDC`, no `MISC_GDI_COMPATIBLE`, no GPU sync barrier.

use windows::Win32::Graphics::Dxgi::{
    IDXGIOutputDuplication, DXGI_OUTDUPL_POINTER_POSITION, DXGI_OUTDUPL_POINTER_SHAPE_INFO,
    DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR, DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR,
    DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME,
};
use windows::Win32::UI::WindowsAndMessaging::{CURSORINFO, CURSOR_SHOWING, GetCursorInfo};

use crate::errors::RustcamError;

#[derive(Clone, Debug)]
pub struct CachedShape {
    /// One of `DXGI_OUTDUPL_POINTER_SHAPE_TYPE_*` (1=mono, 2=color, 4=masked).
    pub kind: u32,
    /// Logical cursor width in pixels.
    pub width: u32,
    /// Logical cursor height in pixels (already halved for mono, since DDA
    /// reports the combined AND+XOR stack height).
    pub height: u32,
    /// Pitch of the source buffer in bytes. For mono this is the byte width
    /// of one plane (the AND or XOR row); for color it's `width * 4`.
    pub pitch: u32,
    /// Raw cursor bitmap as DDA delivered it. For mono cursors the buffer
    /// is `pitch * (height * 2)` bytes: top half is AND mask, bottom half
    /// is XOR mask. For color / masked-color it's `pitch * height` bytes.
    pub pixels: Vec<u8>,
    pub hotspot_x: i32,
    pub hotspot_y: i32,
}

#[derive(Clone, Debug, Default)]
pub struct CursorState {
    pub visible: bool,
    /// Cursor top-left in screen coordinates (NOT hotspot-adjusted; that's
    /// already accounted for by DDA when it reports `Position`).
    pub pos_x: i32,
    pub pos_y: i32,
    pub shape: Option<CachedShape>,
}

impl CursorState {
    /// Called after a successful `AcquireNextFrame`. Updates position from
    /// frame_info and, if the shape buffer changed, refreshes the cache.
    pub unsafe fn update_from_frame(
        &mut self,
        dup: &IDXGIOutputDuplication,
        last_mouse_update_time: i64,
        pp: &DXGI_OUTDUPL_POINTER_POSITION,
        pointer_shape_buffer_size: u32,
    ) -> Result<(), RustcamError> {
        if last_mouse_update_time != 0 {
            // DDA reported a mouse event this frame — use its data (this is
            // the frame-synced position).
            self.visible = pp.Visible.as_bool();
            self.pos_x = pp.Position.x;
            self.pos_y = pp.Position.y;
        } else {
            // DDA had no update this frame. That happens until the user nudges
            // the cursor; the initial state is all zeros so we'd render nothing.
            // Fall back to `GetCursorInfo` — it's a fast registry-style read of
            // the OS cursor state and doesn't move the cursor or trigger any
            // GPU work. Position is reported with the cursor's hotspot
            // already applied, which is the screen-coord top-left we want.
            let mut ci = CURSORINFO {
                cbSize: std::mem::size_of::<CURSORINFO>() as u32,
                ..Default::default()
            };
            if GetCursorInfo(&mut ci).is_ok() {
                self.visible = ci.flags.0 == CURSOR_SHOWING.0;
                // ci.ptScreenPos is the cursor's hotspot. The cached shape's
                // hotspot offset takes us to the cursor top-left.
                let (hx, hy) = self
                    .shape
                    .as_ref()
                    .map(|s| (s.hotspot_x, s.hotspot_y))
                    .unwrap_or((0, 0));
                self.pos_x = ci.ptScreenPos.x - hx;
                self.pos_y = ci.ptScreenPos.y - hy;
            }
        }

        if pointer_shape_buffer_size > 0 {
            let mut buf = vec![0u8; pointer_shape_buffer_size as usize];
            let mut required: u32 = 0;
            let mut info = DXGI_OUTDUPL_POINTER_SHAPE_INFO::default();
            dup.GetFramePointerShape(
                buf.len() as u32,
                buf.as_mut_ptr() as *mut _,
                &mut required,
                &mut info,
            )
            .map_err(RustcamError::Dxgi)?;
            // For mono cursors, DDA's reported Height is AND+XOR combined.
            // The logical cursor height is half of that.
            let kind = info.Type;
            let logical_height = if kind == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME.0 as u32 {
                info.Height / 2
            } else {
                info.Height
            };
            self.shape = Some(CachedShape {
                kind,
                width: info.Width,
                height: logical_height,
                pitch: info.Pitch,
                pixels: buf,
                hotspot_x: info.HotSpot.x,
                hotspot_y: info.HotSpot.y,
            });
        }
        Ok(())
    }
}

/// Composite the cached cursor into a tightly-packed BGRA buffer.
///
/// `dst` is `dst_w * dst_h * 4` bytes, BGRA order, row-major, no padding.
/// `origin_x`/`origin_y` are the screen-coordinate top-left of the dst
/// buffer (i.e. the region's `left`/`top`). The cursor is at screen
/// coordinates `state.pos_x` / `state.pos_y`, so its position relative to
/// the dst buffer is `pos - origin`.
pub fn composite_cursor_into_bgra(
    dst: &mut [u8],
    dst_w: u32,
    dst_h: u32,
    origin_x: i32,
    origin_y: i32,
    state: &CursorState,
) {
    if !state.visible {
        return;
    }
    let shape = match &state.shape {
        Some(s) => s,
        None => return,
    };

    // Cursor top-left relative to dst's top-left.
    let cx = state.pos_x - origin_x;
    let cy = state.pos_y - origin_y;

    // Compute clipped intersection of cursor rect with dst rect.
    let cw = shape.width as i32;
    let ch = shape.height as i32;
    let x_start = cx.max(0);
    let y_start = cy.max(0);
    let x_end = (cx + cw).min(dst_w as i32);
    let y_end = (cy + ch).min(dst_h as i32);
    if x_start >= x_end || y_start >= y_end {
        return;
    }

    let sx_off = (x_start - cx) as u32; // source x offset (start of clip)
    let sy_off = (y_start - cy) as u32;

    let dispatch = match shape.kind {
        k if k == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR.0 as u32 => composite_color,
        k if k == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR.0 as u32 => {
            composite_masked_color
        }
        k if k == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME.0 as u32 => composite_monochrome,
        _ => return,
    };

    dispatch(
        dst,
        dst_w,
        x_start as u32,
        y_start as u32,
        x_end as u32,
        y_end as u32,
        sx_off,
        sy_off,
        shape,
    );
}

type CompositeFn = fn(&mut [u8], u32, u32, u32, u32, u32, u32, u32, &CachedShape);

/// Straight-alpha BGRA composite.
fn composite_color(
    dst: &mut [u8],
    dst_w: u32,
    x0: u32,
    y0: u32,
    x1: u32,
    y1: u32,
    sx_off: u32,
    sy_off: u32,
    shape: &CachedShape,
) {
    let dst_w = dst_w as usize;
    let pitch = shape.pitch as usize;
    let src = &shape.pixels;
    for y in y0..y1 {
        let sy = sy_off + (y - y0);
        let src_row_base = sy as usize * pitch;
        let dst_row_base = y as usize * dst_w * 4;
        for x in x0..x1 {
            let sx = sx_off + (x - x0);
            let s_off = src_row_base + sx as usize * 4;
            let d_off = dst_row_base + x as usize * 4;
            let sb = src[s_off] as u32;
            let sg = src[s_off + 1] as u32;
            let sr = src[s_off + 2] as u32;
            let sa = src[s_off + 3] as u32;
            if sa == 0 {
                continue;
            }
            if sa == 0xFF {
                dst[d_off] = sb as u8;
                dst[d_off + 1] = sg as u8;
                dst[d_off + 2] = sr as u8;
                continue;
            }
            let inv_a = 255 - sa;
            let db = dst[d_off] as u32;
            let dg = dst[d_off + 1] as u32;
            let dr = dst[d_off + 2] as u32;
            // round-to-nearest divide by 255: (x + 127) / 255 ≈ x / 255 within 1 lsb
            dst[d_off] = ((sb * sa + db * inv_a + 127) / 255) as u8;
            dst[d_off + 1] = ((sg * sa + dg * inv_a + 127) / 255) as u8;
            dst[d_off + 2] = ((sr * sa + dr * inv_a + 127) / 255) as u8;
            // alpha channel: leave as-is (desktop alpha = opaque)
        }
    }
}

/// Masked-color: alpha byte is a flag, not a blend factor.
///   alpha == 0x00 → copy src BGR over dst BGR
///   alpha == 0xFF → XOR src BGR into dst BGR (the "inverting" subcase)
fn composite_masked_color(
    dst: &mut [u8],
    dst_w: u32,
    x0: u32,
    y0: u32,
    x1: u32,
    y1: u32,
    sx_off: u32,
    sy_off: u32,
    shape: &CachedShape,
) {
    let dst_w = dst_w as usize;
    let pitch = shape.pitch as usize;
    let src = &shape.pixels;
    for y in y0..y1 {
        let sy = sy_off + (y - y0);
        let src_row_base = sy as usize * pitch;
        let dst_row_base = y as usize * dst_w * 4;
        for x in x0..x1 {
            let sx = sx_off + (x - x0);
            let s_off = src_row_base + sx as usize * 4;
            let d_off = dst_row_base + x as usize * 4;
            let sb = src[s_off];
            let sg = src[s_off + 1];
            let sr = src[s_off + 2];
            let sa = src[s_off + 3];
            if sa == 0 {
                // copy
                dst[d_off] = sb;
                dst[d_off + 1] = sg;
                dst[d_off + 2] = sr;
            } else {
                // XOR
                dst[d_off] ^= sb;
                dst[d_off + 1] ^= sg;
                dst[d_off + 2] ^= sr;
            }
        }
    }
}

/// Monochrome: two 1-bit planes, top half = AND mask, bottom half = XOR mask.
/// For each pixel: `new = (dst AND and_mask) XOR xor_mask`
///   and=1 xor=0 → transparent (keep dst)
///   and=0 xor=0 → black
///   and=0 xor=1 → white
///   and=1 xor=1 → inverted dst   ← the I-beam-over-text case
fn composite_monochrome(
    dst: &mut [u8],
    dst_w: u32,
    x0: u32,
    y0: u32,
    x1: u32,
    y1: u32,
    sx_off: u32,
    sy_off: u32,
    shape: &CachedShape,
) {
    let dst_w = dst_w as usize;
    let pitch = shape.pitch as usize;
    let src = &shape.pixels;
    let and_base = 0usize;
    // The XOR plane starts after `shape.height` rows of the AND plane.
    let xor_base = pitch * shape.height as usize;
    for y in y0..y1 {
        let sy = (sy_off + (y - y0)) as usize;
        let and_row = and_base + sy * pitch;
        let xor_row = xor_base + sy * pitch;
        let dst_row_base = y as usize * dst_w * 4;
        for x in x0..x1 {
            let sx = (sx_off + (x - x0)) as usize;
            let bit = 7 - (sx & 7);
            let and_bit = (src[and_row + (sx >> 3)] >> bit) & 1;
            let xor_bit = (src[xor_row + (sx >> 3)] >> bit) & 1;
            let and_mask: u8 = if and_bit == 0 { 0x00 } else { 0xFF };
            let xor_mask: u8 = if xor_bit == 0 { 0x00 } else { 0xFF };
            let d_off = dst_row_base + x as usize * 4;
            dst[d_off] = (dst[d_off] & and_mask) ^ xor_mask;
            dst[d_off + 1] = (dst[d_off + 1] & and_mask) ^ xor_mask;
            dst[d_off + 2] = (dst[d_off + 2] & and_mask) ^ xor_mask;
        }
    }
}

/// Silence unused-import warning for the CompositeFn type alias (kept for
/// readers; dispatch is via `fn(...) -> ...` literal in the match arms above).
#[allow(dead_code)]
fn _ty_check(_: CompositeFn) {}

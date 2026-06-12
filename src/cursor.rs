//! GDI cursor compositor for the captured BGRA frame.
//!
//! Draws the live OS cursor on top of the DDA-captured BGRA texture via
//! IDXGISurface1::GetDC + DrawIconEx. The capture texture must be created
//! with D3D11_RESOURCE_MISC_GDI_COMPATIBLE.
//!
//! DrawIconEx with DI_NORMAL handles the mask + XOR blending natively, so
//! the inverting I-beam over text composites correctly.

use std::collections::HashMap;
use windows::core::Interface;
use windows::Win32::Graphics::Dxgi::IDXGISurface1;
use windows::Win32::Graphics::Direct3D11::ID3D11Texture2D;
use windows::Win32::Graphics::Gdi::{DeleteObject, HGDIOBJ};
use windows::Win32::UI::WindowsAndMessaging::{
    CURSORINFO, CURSOR_SHOWING, DI_NORMAL, DrawIconEx, GetCursorInfo, GetIconInfo, HCURSOR, HICON,
    ICONINFO,
};

pub type CursorCache = HashMap<usize, (i32, i32)>;

unsafe fn cursor_hotspot(hcur: HCURSOR) -> (i32, i32) {
    let mut ii = ICONINFO::default();
    if GetIconInfo(HICON(hcur.0), &mut ii).is_ok() {
        let hs = (ii.xHotspot as i32, ii.yHotspot as i32);
        if !ii.hbmColor.is_invalid() {
            let _ = DeleteObject(HGDIOBJ(ii.hbmColor.0));
        }
        if !ii.hbmMask.is_invalid() {
            let _ = DeleteObject(HGDIOBJ(ii.hbmMask.0));
        }
        hs
    } else {
        (0, 0)
    }
}

/// Draw the live OS cursor onto a GDI-compatible BGRA D3D11 texture.
///
/// No-op if no cursor is currently showing.
pub unsafe fn draw_cursor(bgra: &ID3D11Texture2D, cache: &mut CursorCache) {
    let mut ci = CURSORINFO {
        cbSize: std::mem::size_of::<CURSORINFO>() as u32,
        ..Default::default()
    };
    if GetCursorInfo(&mut ci).is_err() || ci.flags.0 != CURSOR_SHOWING.0 {
        return;
    }
    let hcur = ci.hCursor;
    if (hcur.0 as usize) == 0 {
        return;
    }
    let key = hcur.0 as usize;
    let hot = match cache.get(&key) {
        Some(h) => *h,
        None => {
            let h = cursor_hotspot(hcur);
            cache.insert(key, h);
            h
        }
    };
    let x = ci.ptScreenPos.x - hot.0;
    let y = ci.ptScreenPos.y - hot.1;
    if let Ok(surface) = bgra.cast::<IDXGISurface1>() {
        if let Ok(hdc) = surface.GetDC(false) {
            let _ = DrawIconEx(hdc, x, y, HICON(hcur.0), 0, 0, 0, None, DI_NORMAL);
            let _ = surface.ReleaseDC(None);
        }
    }
}

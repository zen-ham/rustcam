//! rustcam re-exports the cursor compositor from the shared `dda_capture`
//! crate. The code itself lives there so zentape (and any future Rust
//! consumer of DDA) can use the same implementation, since this was the
//! single biggest fix in the rustcam v0.0.5 → v0.0.6 jump and we don't
//! want to maintain two copies that drift.

pub use dda_capture::cursor::{composite_cursor_into_bgra, CachedShape, CursorState};

# Vendor patches

## xterm.js 5.5.0 (`public/vendor/xterm/lib/xterm.js`)

- **Source**: official xterm.js 5.5.0 (minified) from npm.
- **Patch**: touch handling made unconditional so mobile scrolling works
  inside webpty:
  1. `touchstart` handler no longer checks `areMouseEventsActive` and never
     cancels — plain taps still synthesize mouse events (reasonix clicks).
  2. `touchmove` unconditionally scrolls the viewport (`handleTouchMove`),
     canceling only when the handler says the gesture was consumed.
- **Why**: stock 5.5.0 gates touch scrolling behind
  `areMouseEventsActive`; reasonix/claude TUIs enable mouse events, which
  made the touch path inert and mobile scrollback impossible.
- **How to regenerate**: take the pristine `xterm.js` 5.5.0, locate the
  `addEventListener("touchstart",...)` / `("touchmove",...)` registrations
  in `Viewport`, and apply the two changes above.
- **Integrity**: the test suite asserts the patched bytes are present
  (`test/test_server.py::test_xterm_patch_preserved` via a hash check) so
  an unpatched upgrade fails loudly instead of silently regressing mobile
  scrolling.

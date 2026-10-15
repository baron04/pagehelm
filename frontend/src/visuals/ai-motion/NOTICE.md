# ai-motion

The frontend imports the npm package `ai-motion` 0.4.8 directly (MIT, Simon).
Source: https://github.com/gaomeng1900/ai-motion

The upstream shader, moving sources, default colours, glow depth and theme tuning
are retained. The view disables the opaque border/rounded corners, manages timing,
pauses for reduced motion/hidden tabs, and leaves all page interaction available.

One numerical correction is applied at build time by `frontend/ai-motion.vite.ts`:
`float width = fwidth(d);` becomes `float width = max(fwidth(d), 0.0001);`.
This avoids undefined equal-edge `smoothstep` in antialiasing and the resulting
diagonal speckles reproduced with the unmodified library on Chromium/Metal.
It does not change the palette, motion, glow blending or source positions.

The full upstream MIT license is in `LICENSE` beside this notice and is included
in the shipping runtime bundle's banner.

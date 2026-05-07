#!/usr/bin/env python3
"""
Generate .cube 3D LUT files from the JS LUT functions in dist/index.html.

Source-of-truth math lives in dist/index.html (functions _lutMono, _lutVivid,
_lutDramatic, etc., near line 7340). This script ports those functions to
Python and bakes them into 33×33×33 .cube files that ffmpeg's lut3d filter
consumes server-side. Without these files, the cloud render server can't
apply iPhone LUT-method presets (Vivid, Mono, Noir, Silvertone, Dramatic
+ warm/cool variants) — those would be skipped server-side.

Usage:
    cd infra/render-server
    python3 build_luts.py            # writes 9 *.cube files into ./luts/

After regenerating, deploy to the droplet:
    scp luts/*.cube root@137.184.47.65:/opt/render-server/luts/
    ssh root@137.184.47.65 'systemctl restart render-server'

Regenerate when JS LUT functions in dist/index.html change. Otherwise the
.cube outputs are deterministic — committed to the repo so deploys can ship
them without needing Python on the build pipeline.
"""
import os

GRID = 33   # 33³ = 35,937 grid points. ffmpeg tetrahedral interp gives near-
            # identical results to 65³ at a fraction of the file size and
            # decode cost. 33³ × ~28 bytes/line ≈ 36 KB per .cube file.

# ---------------------------------------------------------------------------
# Helpers — direct ports of dist/index.html lines ~7312–7341
# ---------------------------------------------------------------------------

def clamp_u8(v):
    if v < 0: return 0
    if v > 255: return 255
    return int(v)

def luma(r, g, b):
    # Rec. 709 luminance, matches JS _luma exactly
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def adj_sat(r, g, b, sat):
    """Saturation around luminance. sat=1 passthrough, >1 amplifies, <1 desats."""
    y = luma(r, g, b)
    return [
        clamp_u8(y + (r - y) * sat),
        clamp_u8(y + (g - y) * sat),
        clamp_u8(y + (b - y) * sat),
    ]

def make_curve(pts):
    """Pre-bake a 256-entry tone curve from control points. Linear interpolation
    between adjacent points."""
    out = [0] * 256
    for x in range(256):
        i = 0
        while i < len(pts) - 1 and pts[i+1][0] < x:
            i += 1
        x0, y0 = pts[i]
        x1, y1 = pts[min(i+1, len(pts)-1)]
        t = 0 if x1 == x0 else (x - x0) / (x1 - x0)
        out[x] = clamp_u8(round(y0 + (y1 - y0) * t))
    return out

# Curves — must match dist/index.html control points byte-for-byte
NOIR_CURVE = make_curve([(0,0),(40,15),(96,72),(128,135),(160,200),(224,250),(255,255)])
VIVID_CURVE = make_curve([(0,0),(64,72),(128,138),(192,210),(255,255)])
DRAMATIC_CURVE = make_curve([(0,0),(48,30),(96,80),(128,128),(160,180),(208,225),(255,255)])

# ---------------------------------------------------------------------------
# LUT functions — direct ports of dist/index.html lines ~7344–7396
# ---------------------------------------------------------------------------

def lut_mono(r, g, b):
    """CIPhotoEffectMono — pure Rec. 709 grayscale."""
    y = clamp_u8(round(luma(r, g, b)))
    return [y, y, y]

def lut_silvertone(r, g, b):
    """CIPhotoEffectTonal — luminance with warm tint."""
    y = luma(r, g, b)
    return [clamp_u8(y * 1.10), clamp_u8(y * 1.00), clamp_u8(y * 0.85)]

def lut_noir(r, g, b):
    """CIPhotoEffectNoir — high-contrast B&W with crushed shadows + boosted mids."""
    c = NOIR_CURVE[clamp_u8(round(luma(r, g, b)))]
    return [c, c, c]

def lut_vivid(r, g, b):
    """Vivid — saturation +50% with a midtone-lifting curve."""
    r1, g1, b1 = adj_sat(r, g, b, 1.5)
    return [VIVID_CURVE[r1], VIVID_CURVE[g1], VIVID_CURVE[b1]]

def lut_vivid_warm(r, g, b):
    r1, g1, b1 = lut_vivid(r, g, b)
    return [clamp_u8(r1 * 1.08), clamp_u8(g1 * 1.02), clamp_u8(b1 * 0.88)]

def lut_vivid_cool(r, g, b):
    r1, g1, b1 = lut_vivid(r, g, b)
    return [clamp_u8(r1 * 0.88), clamp_u8(g1 * 1.02), clamp_u8(b1 * 1.10)]

def lut_dramatic(r, g, b):
    """Dramatic — strong S-curve contrast + slight desaturation."""
    r1, g1, b1 = adj_sat(r, g, b, 0.85)
    return [DRAMATIC_CURVE[r1], DRAMATIC_CURVE[g1], DRAMATIC_CURVE[b1]]

def lut_dramatic_warm(r, g, b):
    r1, g1, b1 = lut_dramatic(r, g, b)
    return [clamp_u8(r1 * 1.10), clamp_u8(g1 * 1.04), clamp_u8(b1 * 0.85)]

def lut_dramatic_cool(r, g, b):
    r1, g1, b1 = lut_dramatic(r, g, b)
    return [clamp_u8(r1 * 0.85), clamp_u8(g1 * 1.00), clamp_u8(b1 * 1.12)]

LUTS = {
    'mono':           ('Mono',          lut_mono),
    'silvertone':     ('Silvertone',    lut_silvertone),
    'noir':           ('Noir',          lut_noir),
    'vivid':          ('Vivid',         lut_vivid),
    'vivid-warm':     ('Vivid Warm',    lut_vivid_warm),
    'vivid-cool':     ('Vivid Cool',    lut_vivid_cool),
    'dramatic':       ('Dramatic',      lut_dramatic),
    'dramatic-warm':  ('Dramatic Warm', lut_dramatic_warm),
    'dramatic-cool':  ('Dramatic Cool', lut_dramatic_cool),
}

# ---------------------------------------------------------------------------
# .cube writer — ffmpeg expects R fastest-changing, then G, then B
# ---------------------------------------------------------------------------

def write_cube(filter_id, title, fn, out_path):
    """Write a 33×33×33 .cube file. Iteration order matches the .cube spec:
    outer loop B, middle G, innermost R. ffmpeg's libavfilter/vf_lut3d.c
    expects this ordering."""
    n = GRID
    step = 255.0 / (n - 1)
    lines = [
        f'TITLE "{title}"',
        f'LUT_3D_SIZE {n}',
        'DOMAIN_MIN 0.0 0.0 0.0',
        'DOMAIN_MAX 1.0 1.0 1.0',
    ]
    for b_idx in range(n):
        b_in = round(b_idx * step)
        for g_idx in range(n):
            g_in = round(g_idx * step)
            for r_idx in range(n):
                r_in = round(r_idx * step)
                r_out, g_out, b_out = fn(r_in, g_in, b_in)
                lines.append(f'{r_out/255:.6f} {g_out/255:.6f} {b_out/255:.6f}')
    with open(out_path, 'w') as f:
        f.write('\n'.join(lines))
        f.write('\n')

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, 'luts')
    os.makedirs(out_dir, exist_ok=True)
    for fid, (title, fn) in LUTS.items():
        path = os.path.join(out_dir, f'{fid}.cube')
        write_cube(fid, title, fn, path)
        print(f'  wrote {path}  ({os.path.getsize(path):,} bytes)')
    print(f'\n{len(LUTS)} .cube files written to {out_dir}/')

if __name__ == '__main__':
    main()

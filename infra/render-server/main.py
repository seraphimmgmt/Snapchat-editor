"""
Snap Caption Studio — render server.

POST /render
  Headers: Authorization: Bearer <token>
  Form fields:
    - video: source MP4 file (multipart upload)
    - args: JSON string with render config
    - caption_overlay: optional transparent PNG at output resolution to composite
                       onto every frame via ffmpeg overlay filter.

`args` JSON fields:
    width, height, fps        — output dimensions (default 1080×1920 @ 24)
    target_bitrate            — bits/s (default w*h*fps*0.30, min 8 Mbps)
    max_bitrate, buf_size     — defaults to 1.2× and 2× target
    preset                    — libx264 preset. Default 'fast' (was 'medium' in
                                v4.2.11–v4.2.17). At 15 Mbps target the visual
                                difference is imperceptible but encode is ~2×
                                faster — shaves ~8s off a typical 5s clip.
    adjustments               — { sat, con, bri, war, gra, vig, mirror } sliders
    filters                   — v4.2.24: optional list of CSS-method filter
                                chips. Each entry is {"id": str, "strength": int}
                                where strength is 0..100. Server translates each
                                into a chain of ffmpeg eq / hue / colorchannelmixer
                                ops (sepia/grayscale via interpolated W3C
                                matrices) plus optional color tint blends.
                                LUT-method filters (Vivid, Mono, Noir, etc.) are
                                NOT supported here yet — clients should filter
                                them out before sending. Unknown ids are silently
                                skipped (forward-compat).

Source-of-truth: this file is committed under infra/render-server/main.py and
deployed to the droplet via:
    scp infra/render-server/main.py root@137.184.47.65:/opt/render-server/main.py
    ssh root@137.184.47.65 'systemctl restart render-server'
"""
import json, re, subprocess, uuid, shutil, time
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

AUTH_TOKEN = Path("/opt/render-server/auth_token").read_text().strip()
WORK_DIR = Path("/var/tmp/render-server")
WORK_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

app = FastAPI(title="Snap Caption Studio Render")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

def _check_auth(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    if authorization[7:].strip() != AUTH_TOKEN:
        raise HTTPException(403, "Invalid token")

def _purge_old():
    now = time.time()
    for d in WORK_DIR.glob("*"):
        try:
            if d.is_dir() and now - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# v4.2.24 — Filter chip support (CSS-method, 18 of 27 filters in the library)
# ---------------------------------------------------------------------------
# Mirrors the CSS-method entries in FILTER_LIBRARY in dist/index.html. Each
# entry is (css_filter_string, optional_tint_dict). LUT-method filters
# (Vivid/Mono/Noir/etc.) are deliberately absent — they need .cube LUT files
# on disk + ffmpeg lut3d, scoped for v4.2.25.
#
# Translation reference:
#   saturate(X%)      → eq saturation = X/100  (chains multiplicatively)
#   contrast(X%)      → eq contrast   = X/100
#   brightness(X%)    → eq brightness = (X/100 - 1)  (additive offset, approx
#                       of CSS's multiplier — visual error <2% in tasteful
#                       90–120% range used by all CSSgram presets)
#   hue-rotate(Xdeg)  → hue h = X
#   sepia(X%)         → colorchannelmixer with W3C sepia matrix interpolated
#                       by amount=X/100*strength
#   grayscale(X%)     → colorchannelmixer with W3C grayscale matrix
#                       interpolated by amount=X/100*strength
#   tint              → color source + blend filter, opacity = base_alpha *
#                       strength. CSSgram blend modes map directly to ffmpeg's
#                       blend filter modes (overlay, softlight, multiply,
#                       screen, darken, lighten, burn, dodge, exclusion).
CSS_FILTERS = {
    'clarendon': ('saturate(135%) contrast(120%)',                                            {'hex': '#7FBBE3', 'blend': 'overlay',   'alpha': 0.40}),
    'gingham':   ('brightness(105%) hue-rotate(-10deg)',                                       {'hex': '#E6E6FA', 'blend': 'softlight', 'alpha': 0.40}),
    'moon':      ('grayscale(100%) contrast(110%) brightness(110%)',                          None),
    'lark':      ('contrast(90%) brightness(105%)',                                            {'hex': '#22253F', 'blend': 'burn',      'alpha': 0.20}),
    'reyes':     ('sepia(22%) brightness(110%) contrast(85%) saturate(75%)',                   {'hex': '#ADCDEF', 'blend': 'softlight', 'alpha': 0.50}),
    'juno':      ('hue-rotate(-30deg) contrast(90%) saturate(140%)',                           {'hex': '#7FBBE3', 'blend': 'overlay',   'alpha': 0.40}),
    'slumber':   ('saturate(66%) brightness(105%)',                                            {'hex': '#7D6918', 'blend': 'lighten',   'alpha': 0.40}),
    'crema':     ('sepia(50%) contrast(125%) brightness(115%) saturate(90%) hue-rotate(-5deg)', None),
    'ludwig':    ('sepia(25%) contrast(105%) brightness(105%) saturate(160%)',                 {'hex': '#7D6918', 'blend': 'overlay',   'alpha': 0.10}),
    'aden':      ('hue-rotate(-20deg) contrast(90%) saturate(85%) brightness(120%)',           {'hex': '#420A0E', 'blend': 'darken',    'alpha': 0.20}),
    'perpetua':  ('contrast(105%) saturate(110%)',                                             {'hex': '#005B9A', 'blend': 'softlight', 'alpha': 0.30}),
    'amaro':     ('hue-rotate(-10deg) contrast(90%) brightness(110%) saturate(150%)',          None),
    'mayfair':   ('contrast(110%) saturate(110%)',                                             {'hex': '#BDA681', 'blend': 'overlay',   'alpha': 0.40}),
    'rise':      ('brightness(105%) sepia(20%) contrast(90%) saturate(90%)',                   {'hex': '#E8C598', 'blend': 'overlay',   'alpha': 0.30}),
    'hudson':    ('brightness(120%) contrast(90%) saturate(110%)',                             {'hex': '#A6B1FF', 'blend': 'multiply',  'alpha': 0.30}),
    'valencia':  ('contrast(108%) brightness(108%) sepia(8%)',                                 {'hex': '#3A0339', 'blend': 'exclusion', 'alpha': 0.30}),
    'xpro2':     ('sepia(30%) contrast(110%)',                                                 {'hex': '#E0E7B4', 'blend': 'burn',      'alpha': 0.30}),
    'sierra':    ('contrast(85%) saturate(85%)',                                               {'hex': '#7D6918', 'blend': 'softlight', 'alpha': 0.30}),
    'willow':    ('brightness(90%) contrast(95%) saturate(0%)',                                {'hex': '#D4A9AF', 'blend': 'overlay',   'alpha': 0.40}),
    'lo-fi':     ('saturate(110%) contrast(150%)',                                             None),
    'inkwell':   ('sepia(30%) contrast(110%) brightness(110%) grayscale(100%)',                None),
    'nashville': ('sepia(20%) contrast(120%) brightness(105%) saturate(120%)',                 {'hex': '#F7B099', 'blend': 'darken',    'alpha': 0.50}),
    '1977':      ('contrast(110%) brightness(110%) saturate(130%)',                            {'hex': '#F36ABC', 'blend': 'screen',    'alpha': 0.30}),
    'kelvin':    ('contrast(150%)',                                                            {'hex': '#FF8C28', 'blend': 'dodge',     'alpha': 0.50}),
    'toaster':   ('contrast(150%) brightness(90%)',                                            {'hex': '#B4500F', 'blend': 'screen',    'alpha': 0.40}),
}

_FILTER_OP_RE = re.compile(r'(\w[\w-]*)\(([^)]+)\)')
_VAL_UNIT_RE  = re.compile(r'(-?[\d.]+)\s*([a-z%]*)', re.IGNORECASE)

def _sepia_matrix(amount):
    """W3C sepia matrix interpolated by amount (0..1). amount=0 → identity,
    amount=1 → full sepia. Returns the colorchannelmixer arg string."""
    rr = 1 - amount * 0.607
    rg = amount * 0.769
    rb = amount * 0.189
    gr = amount * 0.349
    gg = 1 - amount * 0.314
    gb = amount * 0.168
    br = amount * 0.272
    bg = amount * 0.534
    bb = 1 - amount * 0.869
    return (f"colorchannelmixer=rr={rr:.3f}:rg={rg:.3f}:rb={rb:.3f}:"
            f"gr={gr:.3f}:gg={gg:.3f}:gb={gb:.3f}:"
            f"br={br:.3f}:bg={bg:.3f}:bb={bb:.3f}")

def _grayscale_matrix(amount):
    """W3C grayscale matrix interpolated by amount. amount=0 → identity,
    amount=1 → luminance-preserving grayscale (Rec. 709 weights)."""
    rr = 1 - amount * 0.7874
    rg = amount * 0.7152
    rb = amount * 0.0722
    gr = amount * 0.2126
    gg = 1 - amount * 0.2848
    gb = amount * 0.0722
    br = amount * 0.2126
    bg = amount * 0.7152
    bb = 1 - amount * 0.9278
    return (f"colorchannelmixer=rr={rr:.3f}:rg={rg:.3f}:rb={rb:.3f}:"
            f"gr={gr:.3f}:gg={gg:.3f}:gb={gb:.3f}:"
            f"br={br:.3f}:bg={bg:.3f}:bb={bb:.3f}")

def _css_filter_to_ffmpeg_ops(css_str, strength):
    """Translate one CSS filter string (e.g. 'saturate(135%) contrast(120%)') to
    a list of ffmpeg filter expressions. `strength` is 0..1 — interpolates each
    op toward its identity value. Multiple saturate/contrast/brightness ops in
    the same filter combine into one eq op (ffmpeg handles them additively/
    multiplicatively in eq's parameter format)."""
    ops = []
    eq_brightness = 0.0   # additive offset
    eq_contrast   = 1.0   # multiplier
    eq_saturation = 1.0   # multiplier
    has_eq_change = False

    for m in _FILTER_OP_RE.finditer(css_str):
        fn = m.group(1).lower()
        arg = m.group(2).strip()
        nm = _VAL_UNIT_RE.match(arg)
        if not nm:
            continue
        try:
            v = float(nm.group(1))
        except ValueError:
            continue

        if fn == 'saturate':
            target = v / 100.0
            eq_saturation *= 1.0 + (target - 1.0) * strength
            has_eq_change = True
        elif fn == 'contrast':
            target = v / 100.0
            eq_contrast *= 1.0 + (target - 1.0) * strength
            has_eq_change = True
        elif fn == 'brightness':
            # CSS brightness is multiplicative; ffmpeg eq.brightness is additive
            # offset (-1..1). Approximate: (target - 1) gives the same direction
            # and roughly the same magnitude in the 0.9–1.2× range that all our
            # CSSgram presets actually use.
            target_mult = v / 100.0
            eq_brightness += (target_mult - 1.0) * strength
            has_eq_change = True
        elif fn == 'hue-rotate':
            ops.append(f"hue=h={v * strength:.2f}")
        elif fn == 'sepia':
            amount = max(0.0, min(1.0, (v / 100.0) * strength))
            if amount > 0.001:
                ops.append(_sepia_matrix(amount))
        elif fn == 'grayscale':
            amount = max(0.0, min(1.0, (v / 100.0) * strength))
            if amount > 0.001:
                ops.append(_grayscale_matrix(amount))
        # Other ops (blur/invert/opacity) aren't used by any CSSgram preset.

    if has_eq_change:
        # Insert eq at the front so it runs before sepia/grayscale matrices —
        # matches CSS evaluation order (filters apply left-to-right) since
        # CSSgram strings always lead with sat/con/bri ops.
        ops.insert(0, f"eq=brightness={eq_brightness:.3f}:"
                      f"contrast={max(0.001, eq_contrast):.3f}:"
                      f"saturation={max(0.0, eq_saturation):.3f}")
    return ops


def _build_chip_ops(filters_list):
    """Given a list of {id, strength} filter chip entries, return:
       (ffmpeg_ops_list, tints_list)
    where tints_list is [{'hex','blend','opacity'}, ...] for blend overlay
    nodes. Unknown / LUT-method ids are silently skipped."""
    all_ops = []
    tints = []
    if not filters_list:
        return all_ops, tints
    for entry in filters_list:
        if not isinstance(entry, dict):
            continue
        fid = entry.get('id')
        try:
            strength = max(0, min(100, int(entry.get('strength', 50)))) / 100.0
        except (TypeError, ValueError):
            strength = 0.5
        if strength <= 0:
            continue
        spec = CSS_FILTERS.get(fid)
        if not spec:
            continue  # unknown id or LUT-method (Vivid/Mono/Noir/etc.) — skip
        css_str, tint = spec
        all_ops.extend(_css_filter_to_ffmpeg_ops(css_str, strength))
        if tint:
            opacity = max(0.0, min(1.0, tint['alpha'] * strength))
            if opacity > 0.001:
                tints.append({
                    'hex': tint['hex'],
                    'blend': tint['blend'],
                    'opacity': opacity,
                })
    return all_ops, tints


def _build_video_filter_chain(w, h, adj):
    if not adj:
        adj = {}
    sat = max(0, min(200, int(adj.get("sat", 100))))
    con = max(50, min(150, int(adj.get("con", 100))))
    bri = max(50, min(150, int(adj.get("bri", 100))))
    war = max(-50, min(50, int(adj.get("war", 0))))
    gra = max(0, min(100, int(adj.get("gra", 0))))
    vig = max(0, min(100, int(adj.get("vig", 0))))

    # Mirror flip: hflip BEFORE other filters so subsequent ops (eq, vignette, etc.)
    # operate on the flipped frame consistently.
    parts = []
    if adj.get('mirror'):
        parts.append('hflip')
    parts.append(f"scale={w}:{h}:flags=lanczos")

    eq_b = (bri - 100) / 100.0
    eq_c = con / 100.0
    eq_s = sat / 100.0
    parts.append(f"eq=brightness={eq_b:.3f}:contrast={eq_c:.3f}:saturation={eq_s:.3f}")

    if war != 0:
        rs = max(-1.0, min(1.0, war / 100.0))
        bs = -rs * 0.6
        parts.append(f"colorbalance=rs={rs:.3f}:gs=0:bs={bs:.3f}")

    if vig > 0:
        angle = 1.5708 - (vig / 100.0) * 0.7854
        parts.append(f"vignette=angle={angle:.3f}")

    if gra > 0:
        intensity = int(gra * 0.4)
        parts.append(f"noise=alls={intensity}:allf=t")

    return ",".join(parts)


def _has_aac_audio(src_path):
    """ffprobe-quick check: does the input have an AAC audio stream we can
    stream-copy? Saves a re-encode pass when source is iPhone/Kling video
    (almost always AAC). Falls back gracefully on probe failure."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "default=nokey=1:noprint_wrappers=1",
             str(src_path)],
            capture_output=True, text=True, timeout=10,
        )
        codec = (proc.stdout or "").strip().lower()
        return codec == "aac"
    except Exception:
        return False


@app.get("/health")
def health():
    try:
        ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        line = ver.stdout.split("\n")[0] if ver.stdout else "unknown"
        return {"ok": True, "ffmpeg": line, "filter_chip_support": True, "css_filter_count": len(CSS_FILTERS)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/render")
async def render(
    video: UploadFile = File(...),
    args: str = Form(...),
    caption_overlay: UploadFile = File(None),
    authorization: str = Header(None),
):
    _check_auth(authorization)
    _purge_old()

    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir()

    try:
        src_path = job_dir / "source.mp4"
        out_path = job_dir / "output.mp4"
        overlay_path = job_dir / "caption.png"

        size = 0
        with src_path.open("wb") as f:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Source video too large (>500 MB)")
                f.write(chunk)

        has_overlay = False
        if caption_overlay and caption_overlay.filename:
            osize = 0
            with overlay_path.open("wb") as f:
                while True:
                    chunk = await caption_overlay.read(1024 * 1024)
                    if not chunk: break
                    osize += len(chunk)
                    if osize > 50 * 1024 * 1024:
                        raise HTTPException(413, "Caption overlay too large (>50 MB)")
                    f.write(chunk)
            has_overlay = osize > 0

        try:
            cfg = json.loads(args)
        except Exception:
            raise HTTPException(400, "args must be valid JSON")

        w = int(cfg.get("width", 1080))
        h = int(cfg.get("height", 1920))
        fps = int(cfg.get("fps", 24))
        target = int(cfg.get("target_bitrate", max(int(w * h * fps * 0.30), 8_000_000)))
        maxr = int(cfg.get("max_bitrate", target * 1.2))
        buf = int(cfg.get("buf_size", target * 2))
        # v4.2.18: default 'fast' (was 'medium'). At 15 Mbps target the
        # visual difference is imperceptible but encode is ~2× faster.
        preset = cfg.get("preset", "fast")
        keyint = int(cfg.get("keyint", max(24, fps * 2)))
        adjustments = cfg.get("adjustments", {})
        # v4.2.24: filter chips. Each entry is {id, strength}.
        chip_filters = cfg.get("filters", [])
        if not isinstance(chip_filters, list):
            chip_filters = []

        vf_main = _build_video_filter_chain(w, h, adjustments)

        # Filter chip ops — appended to the main chain; tints applied as
        # filter_complex blend nodes after the main chain finishes.
        chip_ops, chip_tints = _build_chip_ops(chip_filters)
        if chip_ops:
            vf_main = vf_main + "," + ",".join(chip_ops)

        # v4.2.18: if source audio is already AAC, stream-copy it instead of
        # re-encoding. Saves 2-5s on typical iPhone/Kling clips. Falls back to
        # AAC re-encode for non-AAC sources (Opus/Vorbis/etc).
        audio_aac = _has_aac_audio(src_path)
        audio_args = ["-c:a", "copy"] if audio_aac else ["-c:a", "aac", "-b:a", "192k"]

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src_path)]
        if has_overlay:
            cmd += ["-i", str(overlay_path)]

        # Decide whether to use simple -vf or filter_complex. We need
        # filter_complex if there's a caption overlay (input 1) or any tints
        # (color sources via filter_complex internal nodes).
        if has_overlay or chip_tints:
            fc_parts = [f"[0:v]{vf_main}[v0]"]
            last_label = "v0"
            for i, tint in enumerate(chip_tints):
                next_label = f"vt{i}"
                color_label = f"c{i}"
                # color source — filter_complex internal node, no extra -i input.
                # CRITICAL perf settings:
                #   r={fps}   match main video's framerate. Default color rate
                #             is 25fps; main is typically 30. Without matching
                #             rates ffmpeg does costly rate conversion that
                #             tanked render speed ~25× in early tests.
                #   d=1       only emit one second's worth of color frames.
                #             blend's repeatlast=1 reuses the last frame for
                #             every subsequent main frame — so color stops
                #             generating at 1s while blend keeps producing
                #             output frames as long as main input continues.
                #             Wastes 30 throwaway frames of color generation
                #             upfront (negligible) and avoids per-frame color
                #             regeneration for the rest of the clip.
                fc_parts.append(f"color=c={tint['hex']}:s={w}x{h}:r={fps}:d=1[{color_label}]")
                # blend opts:
                #   shortest=0   (default) — blend continues as long as the
                #                MAIN input has frames. We deliberately don't
                #                set shortest=1 because that would truncate
                #                output to color's 1-second duration.
                #   repeatlast=1 (default) — after color EOFs, blend reuses
                #                its last frame against every main frame.
                #                This is what makes the d=1 trick work.
                fc_parts.append(
                    f"[{last_label}][{color_label}]"
                    f"blend=all_mode={tint['blend']}:all_opacity={tint['opacity']:.3f}"
                    f"[{next_label}]"
                )
                last_label = next_label
            if has_overlay:
                next_label = "vout"
                fc_parts.append(f"[{last_label}][1:v]overlay=0:0:format=auto[{next_label}]")
                last_label = next_label
            fc = ";".join(fc_parts)
            cmd += [
                "-filter_complex", fc,
                "-map", f"[{last_label}]",
                "-map", "0:a?",
            ]
        else:
            cmd += ["-vf", vf_main]
        cmd += [
            "-c:v", "libx264", "-preset", preset,
            "-b:v", str(target), "-maxrate", str(maxr), "-bufsize", str(buf),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv",
            "-g", str(keyint),
        ] + audio_args + [
            str(out_path),
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise HTTPException(500, f"ffmpeg failed (rc={proc.returncode}): {proc.stderr[:1500]}")

        if not out_path.exists() or out_path.stat().st_size < 1024:
            raise HTTPException(500, "ffmpeg produced empty output")

        return FileResponse(
            str(out_path),
            media_type="video/mp4",
            filename=f"render_{job_id}.mp4",
        )
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, str(e))

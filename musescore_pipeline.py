"""
musescore_pipeline.py
══════════════════════
Handles the MuseScore → PNG/SVG/MSCX pipeline portably.

Responsibilities:
  1. Locate mscore3/mscore/MuseScore executable on any platform
  2. Extract + patch .mscz files (fix version mismatch)
  3. Render to PNG + SVG + MIDI via MuseScore headless
  4. Return job config ready to pass to ScoreEngine

Usage:
    from musescore_pipeline import process_mscz_files
    cfg = process_mscz_files(
        mscz_paths=["/path/to/1.mscz", "/path/to/2.mscz"],
        workdir="/tmp/job_abc123",
        progress_cb=lambda pct, msg: print(f"{pct}% {msg}")
    )
    # cfg is a dict ready for build_engine(cfg)
"""

import os, sys, re, zipfile, subprocess, platform, shutil, tempfile
from pathlib import Path

# ─── MuseScore locator ────────────────────────────────────────────────────────

MSCORE_CANDIDATES = {
    "windows": [
        r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
        r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe",
        r"C:\Program Files (x86)\MuseScore 3\bin\MuseScore3.exe",
        # Portable (bundled with app)
        os.path.join(os.path.dirname(__file__), "musescore_portable", "MuseScore3.exe"),
    ],
    "darwin": [
        "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
        "/Applications/MuseScore 3.app/Contents/MacOS/mscore",
        "/usr/local/bin/mscore3",
        "/opt/homebrew/bin/mscore3",
    ],
    "linux": [
        "/usr/bin/mscore3",
        "/usr/bin/mscore",
        "/usr/local/bin/mscore3",
        shutil.which("mscore3") or "",
        shutil.which("mscore") or "",
    ],
}

def find_musescore():
    """Return path to MuseScore executable, or raise RuntimeError."""
    plat = platform.system().lower()
    candidates = MSCORE_CANDIDATES.get(plat, MSCORE_CANDIDATES["linux"])
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise RuntimeError(
        "MuseScore not found. Please install MuseScore 3 or 4 from https://musescore.org"
    )

def _run_mscore(mscore_bin, args, workdir=None):
    """Run MuseScore headlessly. Uses xvfb-run on Linux if no display."""
    cmd = [mscore_bin] + args
    plat = platform.system().lower()
    if plat == "linux":
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            cmd = [xvfb, "--auto-servernum"] + cmd
    env = os.environ.copy()
    if plat == "linux":
        env.setdefault("XDG_RUNTIME_DIR", "/tmp")
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env,
        cwd=workdir or tempfile.gettempdir()
    )
    return result

# ─── .mscz extraction + patching ─────────────────────────────────────────────

def _extract_and_patch_mscz(mscz_path, mscx_dir):
    """
    Extract .mscx from .mscz, patch version string so MuseScore 3 accepts it,
    save to mscx_dir. Returns (mscx_path, base_name).
    """
    mscz_path = Path(mscz_path)
    base = mscz_path.stem          # e.g. "1-MySong" or "intro"
    mscx_name = base + ".mscx"

    with zipfile.ZipFile(mscz_path) as z:
        # Find the .mscx inside (name may differ from zip filename in older versions)
        mscx_members = [n for n in z.namelist() if n.endswith(".mscx")]
        if not mscx_members:
            raise ValueError(f"No .mscx found inside {mscz_path}")
        content = z.read(mscx_members[0]).decode("utf-8")

    # Patch version so MuseScore 3.2 accepts files saved by 3.6
    content = re.sub(r'version="3\.\d+"', 'version="3.01"', content)
    content = re.sub(r'<programVersion>[^<]+</programVersion>',
                     '<programVersion>3.2.3</programVersion>', content)

    out_path = Path(mscx_dir) / mscx_name
    out_path.write_text(content, encoding="utf-8")
    return str(out_path), base

# ─── main pipeline ────────────────────────────────────────────────────────────

def process_mscz_files(mscz_paths, workdir, progress_cb=None):
    """
    Full pipeline: .mscz list → rendered assets → engine config dict.

    Args:
        mscz_paths:  ordered list of .mscz file paths
        workdir:     temp directory for this job (created if absent)
        progress_cb: optional callable(percent:int, message:str)

    Returns:
        dict  ready to pass directly to score_engine.build_engine()
    """
    def _prog(pct, msg):
        if progress_cb: progress_cb(pct, msg)

    os.makedirs(workdir, exist_ok=True)
    mscx_dir = os.path.join(workdir, "mscx")
    png_dir  = os.path.join(workdir, "png")
    svg_dir  = os.path.join(workdir, "svg")
    for d in [mscx_dir, png_dir, svg_dir]:
        os.makedirs(d, exist_ok=True)

    _prog(2, "Locating MuseScore…")
    mscore = find_musescore()

    n = len(mscz_paths)
    file_nums = list(range(1, n + 1))
    name_tpl  = "{i}-score"   # internal naming: 1-score, 2-score …

    # Phase 1: extract + patch
    for idx, mscz_path in enumerate(mscz_paths):
        _prog(5 + idx * 5 // n, f"Extracting {Path(mscz_path).name}…")
        mscx_path, _ = _extract_and_patch_mscz(mscz_path, mscx_dir)
        # Rename to canonical template name
        canonical = os.path.join(mscx_dir, f"{idx+1}-score.mscx")
        if mscx_path != canonical:
            shutil.move(mscx_path, canonical)

    # Phase 2: render PNG + SVG (one MuseScore call per file)
    for idx, i in enumerate(file_nums):
        base_pct = 15 + idx * 60 // n
        _prog(base_pct, f"Rendering page {i}/{n}…")
        mscx = os.path.join(mscx_dir, f"{i}-score.mscx")

        # PNG
        png_out = os.path.join(png_dir, f"{i}-score.png")
        r = _run_mscore(mscore, ["-o", png_out, mscx])
        if r.returncode != 0 and "success" not in r.stdout.lower():
            raise RuntimeError(f"MuseScore PNG render failed for page {i}:\n{r.stderr[:500]}")

        # SVG
        svg_out = os.path.join(svg_dir, f"{i}-score.svg")
        r = _run_mscore(mscore, ["-o", svg_out, mscx])
        if r.returncode != 0 and "success" not in r.stdout.lower():
            raise RuntimeError(f"MuseScore SVG render failed for page {i}:\n{r.stderr[:500]}")

        _prog(base_pct + 55 // n, f"Page {i}/{n} rendered ✓")

    _prog(80, "Pipeline complete — building engine config…")

    # Return engine config dict (all defaults, paths filled in)
    return {
        "mscx_dir":  mscx_dir,
        "png_dir":   png_dir,
        "svg_dir":   svg_dir,
        "file_nums": file_nums,
        "name_tpl":  "{i}-score",
    }

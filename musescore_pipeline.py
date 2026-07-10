"""
musescore_pipeline.py
══════════════════════
Handles the MuseScore → PNG/SVG/MSCX pipeline portably.

Responsibilities:
  1. Locate mscore3/mscore/MuseScore executable on any platform
  2. Extract + patch .mscz files (fix version mismatch)
  3. Render to PNG + SVG via MuseScore headless
  4. Return job config ready to pass to ScoreEngine

Usage:
    from musescore_pipeline import process_mscz_files
    from progress import Progress
    cfg = process_mscz_files(
        mscz_paths=["/path/to/1.mscz", "/path/to/2.mscz"],
        workdir="/tmp/job_abc123",
        progress=Progress(web_cb),   # barras de consola + % de la web
    )
    # cfg is a dict ready for build_engine(cfg)
"""

import os, re, zipfile, subprocess, platform, shutil, tempfile
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
        "No se encontró MuseScore. Instalá MuseScore 3 o 4 desde https://musescore.org"
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

    # Solo los archivos de MuseScore 3 necesitan el parche de versión (para
    # que 3.2 acepte lo guardado por 3.6). Los de MuseScore 4 se dejan
    # intactos: los abre MuseScore 4 y tocarles programVersion es dañino.
    ver = re.search(r'<museScore version="(\d+)\.', content)
    if ver and ver.group(1) == "3":
        content = re.sub(r'version="3\.\d+"', 'version="3.01"', content)
        content = re.sub(r'<programVersion>[^<]+</programVersion>',
                         '<programVersion>3.2.3</programVersion>', content)

    out_path = Path(mscx_dir) / mscx_name
    out_path.write_text(content, encoding="utf-8")
    return str(out_path), base

def _check_single_page(out_dir, i, ext):
    """El motor asume una hoja por .mscz: verifica que MuseScore haya escrito
    exactamente `{i}-score-1.{ext}` y avisa con claridad si el archivo tiene
    más de una hoja (el contenido extra se perdería en silencio)."""
    first = os.path.join(out_dir, f"{i}-score-1.{ext}")
    if not os.path.isfile(first):
        raise RuntimeError(
            f"MuseScore no generó la salida esperada para la página {i} ({ext}).")
    if os.path.isfile(os.path.join(out_dir, f"{i}-score-2.{ext}")):
        raise RuntimeError(
            f"El archivo {i} tiene más de una hoja. Esta app espera un .mscz "
            "por hoja: dividí la partitura en un archivo por página y volvé a subirlos.")


# ─── main pipeline ────────────────────────────────────────────────────────────

# Resolución (DPI) del PNG exportado. Explícita SIEMPRE: algunas versiones de
# MuseScore exportan a resoluciones enormes (>130 megapíxeles por hoja), lo que
# dispara advertencias de PIL y congela el pipeline minutos enteros al abrir y
# reescalar las imágenes. 300 DPI ≈ 2500 px de ancho en A4: nítido incluso para
# video 4K y órdenes de magnitud más rápido de procesar.
PNG_DPI = 300


def process_mscz_files(mscz_paths, workdir, progress=None):
    """
    Full pipeline: .mscz list → rendered assets → engine config dict.

    Args:
        mscz_paths: ordered list of .mscz file paths
        workdir:    temp directory for this job (created if absent)
        progress:   objeto progress.Progress del trabajo (opcional). Cada fase
                    del pipeline dibuja su propia barra en consola — ver la
                    guía en progress.py antes de agregar pasos nuevos.

    Returns:
        dict  ready to pass directly to score_engine.build_engine()
    """
    from progress import Progress
    progress = progress or Progress()

    os.makedirs(workdir, exist_ok=True)
    mscx_dir = os.path.join(workdir, "mscx")
    png_dir  = os.path.join(workdir, "png")
    svg_dir  = os.path.join(workdir, "svg")
    for d in [mscx_dir, png_dir, svg_dir]:
        os.makedirs(d, exist_ok=True)

    n = len(mscz_paths)
    file_nums = list(range(1, n + 1))

    # Phase 1: extract + patch
    with progress.phase("Preparando partituras", span=(2, 8)) as ph:
        mscore = find_musescore()
        for idx, mscz_path in enumerate(mscz_paths):
            mscx_path, _ = _extract_and_patch_mscz(mscz_path, mscx_dir)
            # Rename to canonical template name
            canonical = os.path.join(mscx_dir, f"{idx+1}-score.mscx")
            if mscx_path != canonical:
                shutil.move(mscx_path, canonical)
            ph.update((idx + 1) / n, Path(mscz_path).name)

    # Phase 2: render PNG + SVG (one MuseScore call per file)
    with progress.phase("Renderizando páginas (MuseScore)", span=(8, 72)) as ph:
        for idx, i in enumerate(file_nums):
            mscx = os.path.join(mscx_dir, f"{i}-score.mscx")

            ph.update((idx * 2) / (n * 2), f"página {i}/{n} · PNG")
            png_out = os.path.join(png_dir, f"{i}-score.png")
            r = _run_mscore(mscore, ["-o", png_out, "-r", str(PNG_DPI), mscx])
            if r.returncode != 0 and "success" not in r.stdout.lower():
                raise RuntimeError(f"MuseScore falló renderizando el PNG de la página {i}:\n{r.stderr[:500]}")
            _check_single_page(png_dir, i, "png")

            ph.update((idx * 2 + 1) / (n * 2), f"página {i}/{n} · SVG")
            svg_out = os.path.join(svg_dir, f"{i}-score.svg")
            r = _run_mscore(mscore, ["-o", svg_out, mscx])
            if r.returncode != 0 and "success" not in r.stdout.lower():
                raise RuntimeError(f"MuseScore falló renderizando el SVG de la página {i}:\n{r.stderr[:500]}")
            _check_single_page(svg_dir, i, "svg")
            ph.update((idx + 1) / n, f"página {i}/{n} ✓")

    # Return engine config dict (all defaults, paths filled in)
    return {
        "mscx_dir":  mscx_dir,
        "png_dir":   png_dir,
        "svg_dir":   svg_dir,
        "file_nums": file_nums,
        "name_tpl":  "{i}-score",
    }

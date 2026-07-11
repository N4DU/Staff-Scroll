import os
import re
import uuid
import shutil
import threading
import subprocess
import tempfile
from flask import Flask, request, jsonify, render_template, Response, send_file
from werkzeug.utils import secure_filename


jobs = {}
_jobs_lock = threading.Lock()

# Formatos de audio aceptados para la pista de fondo. ffmpeg (ya requerido
# por la app) decodifica todos estos sin problema.
AUDIO_EXTS = {".mp3", ".mpeg", ".mpga", ".wav", ".m4a", ".aac", ".ogg",
              ".oga", ".opus", ".flac", ".wma", ".aiff", ".aif", ".webm"}


def create_app():
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/upload", methods=["POST"])
    def upload():
        files = request.files.getlist("files")
        if not files or all(f.filename == "" for f in files):
            return jsonify({"error": "No se recibieron archivos"}), 400

        job_id = uuid.uuid4().hex[:10]
        job_dir = os.path.join(tempfile.gettempdir(), f"scrolling_score_{job_id}")
        mscz_dir = os.path.join(job_dir, "uploads")
        os.makedirs(mscz_dir, exist_ok=True)

        saved_paths = []
        for idx, f in enumerate(files):
            if not f.filename.lower().endswith(".mscz"):
                return jsonify({"error": f"Archivo no válido: {f.filename}"}), 400
            # secure_filename evita path traversal; el prefijo numérico evita
            # colisiones entre archivos con el mismo nombre y fija el orden.
            safe = secure_filename(f.filename) or f"page{idx + 1}.mscz"
            dest = os.path.join(mscz_dir, f"{idx + 1:03d}-{safe}")
            f.save(dest)
            saved_paths.append(dest)

        # Audio de fondo opcional (la canción real, para el editor de sync)
        audio_path = None
        audio = request.files.get("audio")
        if audio and audio.filename:
            ext = os.path.splitext(audio.filename)[1].lower()
            if ext not in AUDIO_EXTS:
                return jsonify({"error": f"Formato de audio no soportado: {ext}"}), 400
            audio_path = os.path.join(job_dir, f"song{ext}")
            audio.save(audio_path)

        jobs[job_id] = {
            "pct": 0,
            "msg": "Archivos subidos correctamente",
            "done": False,
            "error": None,
            "output": None,
            "mscz_paths": saved_paths,
            "audio_path": audio_path,
            "workdir": job_dir,
        }
        return jsonify({"job_id": job_id})

    @app.route("/generate", methods=["POST"])
    def generate():
        data = request.get_json(force=True)
        job_id = data.get("job_id")
        options = data.get("options", {})

        if not job_id or job_id not in jobs:
            return jsonify({"error": "Job no encontrado"}), 404

        job = jobs[job_id]
        with _jobs_lock:
            if job.get("started"):
                return jsonify({"error": "Ya está en proceso"}), 409
            job["started"] = True

        t = threading.Thread(target=_run_job, args=(job_id, options), daemon=True)
        t.start()
        return jsonify({"status": "iniciado"})

    @app.route("/progress/<job_id>")
    def progress(job_id):
        def stream():
            import time, json
            while True:
                if job_id not in jobs:
                    yield f"data: {json.dumps({'pct': 0, 'msg': 'Job no encontrado', 'done': True, 'error': 'not_found'})}\n\n"
                    break
                j = jobs[job_id]
                payload = {"pct": j["pct"], "msg": j["msg"], "done": j["done"],
                           "error": j["error"], "editor": bool(j.get("editor"))}
                yield f"data: {json.dumps(payload)}\n\n"
                if j["done"]:
                    break
                time.sleep(0.4)

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/download/<job_id>")
    def download(job_id):
        if job_id not in jobs:
            return jsonify({"error": "Job no encontrado"}), 404
        j = jobs[job_id]
        if not j["done"] or j["error"]:
            return jsonify({"error": "Video no disponible"}), 425
        out = j.get("output")
        if not out or not os.path.isfile(out):
            return jsonify({"error": "Archivo no encontrado"}), 404

        def _cleanup():
            # Margen amplio para que send_file termine de transmitir el video
            # antes de borrar el directorio de trabajo.
            import time as _t
            _t.sleep(120)
            shutil.rmtree(j["workdir"], ignore_errors=True)
            jobs.pop(job_id, None)

        threading.Thread(target=_cleanup, daemon=True).start()
        return send_file(out, as_attachment=True, download_name="scrolling_score.mp4", mimetype="video/mp4")

    # ── rutas del editor de sincronización ───────────────────────────────────

    @app.route("/page/<job_id>/<int:n>")
    def score_page(job_id, n):
        """Hoja original de la partitura (PNG renderizado por MuseScore)."""
        j = jobs.get(job_id)
        if not j or not j.get("png_dir"):
            return jsonify({"error": "Página no disponible"}), 404
        path = os.path.join(j["png_dir"], f"{n}-score-1.png")
        if not os.path.isfile(path):
            return jsonify({"error": "Página no disponible"}), 404
        return send_file(path, mimetype="image/png", conditional=True)

    @app.route("/audio/<job_id>")
    def preview_audio(job_id):
        j = jobs.get(job_id)
        if not j or not j.get("audio_path") or not os.path.isfile(j["audio_path"]):
            return jsonify({"error": "Audio no disponible"}), 404
        import mimetypes
        mt = mimetypes.guess_type(j["audio_path"])[0] or "audio/mpeg"
        if j["audio_path"].lower().endswith((".mpeg", ".mpga")):
            mt = "audio/mpeg"  # nuestros .mpeg son MP3 de audio, no video
        return send_file(j["audio_path"], mimetype=mt, conditional=True)

    @app.route("/editor_data/<job_id>")
    def editor_data(job_id):
        j = jobs.get(job_id)
        if not j or not j.get("editor_data"):
            return jsonify({"error": "Datos del editor no disponibles"}), 404
        return jsonify(j["editor_data"])

    @app.route("/finalize/<job_id>", methods=["POST"])
    def finalize(job_id):
        j = jobs.get(job_id)
        if not j or not j.get("done") or j.get("error") or not j.get("render"):
            return jsonify({"error": "Job no está listo"}), 409
        data = request.get_json(force=True)
        skip = bool(data.get("skip"))
        try:
            # offset negativo = la partitura empieza ANTES que el audio
            offset  = max(-7200.0, min(7200.0, float(data.get("offset", 0.0))))
            stretch = max(0.5, min(2.0, float(data.get("stretch", 1.0))))
            # correcciones de pulsos hechas en el editor con [P]:
            # {i: índice de compás en la línea de tiempo, k: pulso, x: fracción}
            fixes = []
            for f in (data.get("pulse_fixes") or [])[:4000]:
                fixes.append({"i": int(f["i"]), "k": int(f["k"]),
                              "x": min(1.0, max(0.0, float(f["x"])))})
        except (TypeError, ValueError, KeyError):
            return jsonify({"error": "Parámetros inválidos"}), 400
        j["pulse_fixes"] = fixes

        with _jobs_lock:
            if j.get("finalizing"):
                return jsonify({"error": "Ya se está generando el video final"}), 409
            j["finalizing"] = True
        j["done"] = False
        j["error"] = None
        j["editor"] = False
        j["pct"] = 5
        j["msg"] = "Preparando el video final…"

        threading.Thread(target=_run_finalize, args=(job_id, offset, stretch, skip),
                         daemon=True).start()
        return jsonify({"status": "iniciado"})

    return app


# ─── job runner ──────────────────────────────────────────────────────────────
#
# El progreso se reporta por PROCEDIMIENTO: cada paso del pipeline tiene su
# propia barra en la consola (y aporta un tramo del % global de la web).
# Antes de agregar un paso nuevo, leé la guía en progress.py — agregar la
# barrita de tu paso son 3 líneas (`with progress.phase(...)`).

def _run_job(job_id, options):
    from musescore_pipeline import process_mscz_files
    from score_engine import build_engine
    from progress import Progress

    j = jobs[job_id]

    def prog(pct, msg):
        j["pct"] = pct
        j["msg"] = msg

    progress = Progress(web_cb=prog)
    j["progress"] = progress
    n_scores = len(j["mscz_paths"])
    audio_note = " + canción" if j.get("audio_path") else ""
    progress.announce(f"♪ Nuevo trabajo {job_id} — "
                      f"{n_scores} partitura{'s' if n_scores != 1 else ''}{audio_note}")

    try:
        prog(2, "Iniciando pipeline…")
        workdir    = j["workdir"]
        mscz_paths = j["mscz_paths"]
        render_dir = os.path.join(workdir, "render")

        # Fases 1-2: pipeline de MuseScore (extraer + renderizar, 2% → 72%)
        engine_cfg = process_mscz_files(
            mscz_paths=mscz_paths,
            workdir=render_dir,
            progress=progress,
        )

        # Merge user options into engine config
        if options.get("show_header") is False:
            engine_cfg["show_header"] = False
        if "page_gap_pct" in options:
            engine_cfg["page_gap_pct"] = min(100.0, max(0.0, float(options["page_gap_pct"])))
        if "playhead_frac" in options:
            engine_cfg["playhead_frac"] = min(1.0, max(0.0, float(options["playhead_frac"])))
        if "count_in_beats" in options:
            engine_cfg["count_in_beats"] = max(0, min(16, int(options["count_in_beats"])))
        if options.get("song_name"):
            engine_cfg["song_name"] = str(options["song_name"])

        # Línea de tiempo (playhead): estilo configurable
        if options.get("playhead_mode") in ("fluid", "beats"):
            engine_cfg["playhead_mode"] = options["playhead_mode"]
        col = options.get("playhead_color")
        if isinstance(col, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", col):
            c = col.lstrip("#")
            engine_cfg["playhead_color"] = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
        if "playhead_alpha" in options:
            engine_cfg["playhead_alpha"] = min(1.0, max(0.05, float(options["playhead_alpha"])))
        if "playhead_w" in options:
            engine_cfg["playhead_w"] = max(1, min(8, int(options["playhead_w"])))
        if options.get("show_playhead") is False:
            engine_cfg["playhead_w"] = 0

        # Resolución del dispositivo destino (par: se exige para yuv420p)
        if options.get("video_w") and options.get("video_h"):
            try:
                w = max(320, min(3840, int(options["video_w"])))
                h = max(240, min(2160, int(options["video_h"])))
                engine_cfg["video_w"] = w - (w % 2)
                engine_cfg["video_h"] = h - (h % 2)
            except (TypeError, ValueError):
                pass

        with progress.phase("Construyendo motor de video", span=(72, 82)) as ph:
            engine = build_engine(engine_cfg, phase=ph)

        j["engine"]         = engine
        j["fps"]            = engine_cfg.get("fps", 30)
        j["png_dir"]        = engine_cfg["png_dir"]
        j["lead_in"]        = getattr(engine, "lead_in", 0.0)
        j["video_duration"] = engine.total_duration

        if j.get("audio_path"):
            # Con audio: el editor abre YA (no necesita el video) y el render
            # corre en segundo plano mientras el usuario alinea.
            from audio_sync import analyze_audio
            with progress.phase("Analizando la canción", span=(82, 98)) as ph:
                analysis = analyze_audio(_find_ffmpeg(), j["audio_path"], phase=ph)
            j["editor_data"] = _build_editor_data(engine, analysis)
            j["render"] = {"pct": 0, "done": False, "error": None, "path": None}
            threading.Thread(target=_render_silent, args=(job_id,), daemon=True).start()
            j["editor"] = True
            j["pct"]  = 100
            j["msg"]  = "Partitura lista — abriendo editor de sincronización…"
            j["done"] = True
            progress.announce("  Editor de sincronización listo — el video se "
                              "renderiza en segundo plano.")
            return

        # Sin audio: renderizar directo como siempre
        with progress.phase("Renderizando el video", span=(82, 99)) as ph:
            out_path = _render_video_frames(
                j, engine, lambda p: ph.update(p / 100))
        j["output"] = out_path
        j["pct"]    = 100
        j["msg"]    = "¡Video listo para descargar!"
        j["done"]   = True
        progress.announce("  ✔ Video listo para descargar desde la página.")

    except Exception as exc:
        progress.error(exc)
        j["pct"]   = 0
        j["msg"]   = f"Error: {exc}"
        j["error"] = str(exc)
        j["done"]  = True


def _render_video_frames(j, engine, report):
    """Renderiza el video mudo (frames → ffmpeg). report(pct 0-100)."""
    fps      = j["fps"]
    n_frames = max(1, int(engine.total_duration * fps))
    W, H     = engine.video_w, engine.video_h
    out_path = os.path.join(j["workdir"], "scrolling_score.mp4")

    cmd = [
        _find_ffmpeg(), "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "bgr24",
        "-r", str(fps), "-i", "pipe:0",
        "-vcodec", "libx264", "-preset", "fast",
        "-crf", "18", "-pix_fmt", "yuv420p",
        out_path,
    ]
    # stderr va a un archivo: con stderr=PIPE sin lector, el buffer se llena
    # y ffmpeg + este proceso quedan bloqueados (deadlock).
    stderr_path = os.path.join(j["workdir"], "ffmpeg_stderr.log")
    with open(stderr_path, "wb") as errf:
        pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
        try:
            for i in range(n_frames):
                frame = engine.render_frame(i / fps)
                pipe.stdin.write(frame.tobytes())
                if i % max(1, n_frames // 100) == 0:
                    report(int(i * 100 / n_frames))
            pipe.stdin.close()
        except BrokenPipeError:
            pass  # ffmpeg murió a mitad de camino; el returncode lo dirá
        pipe.wait()

    if pipe.returncode != 0:
        with open(stderr_path, encoding="utf-8", errors="replace") as f:
            err = f.read()
        raise RuntimeError(f"ffmpeg falló:\n{err[-600:]}")
    return out_path


def _render_silent(job_id):
    """Render en segundo plano mientras el usuario usa el editor."""
    from progress import Progress
    j = jobs[job_id]
    r = j["render"]
    progress = j.get("progress") or Progress()
    try:
        # span=None: esta barra vive solo en la consola; la barra de la web la
        # maneja _run_finalize cuando el usuario confirma la sincronización.
        with progress.phase("Renderizando el video (2º plano)", span=None) as ph:
            def _report(p):
                r["pct"] = p
                ph.update(p / 100)
            r["path"] = _render_video_frames(j, j["engine"], _report)
        r["pct"]  = 100
        r["done"] = True
    except Exception as exc:
        r["error"] = str(exc)
        r["done"]  = True


def _build_editor_data(engine, analysis):
    """Datos para el editor: compases con tiempo musical y geometría sobre la
    hoja original (fracciones del PNG → independiente de la resolución)."""
    lead = getattr(engine, "lead_in", 0.0)
    pad = 25  # unidades SVG extra alrededor del sistema para el cursor
    measures = []
    for (t, dur, fn, mi, beats, bpm) in engine._timeline:
        si, x0, x1 = engine.measure_map[fn][mi]
        lay = engine.layouts[fn]
        bmap = engine.beat_x_map[fn][mi]
        measures.append({
            "t": round(t - lead, 4), "dur": round(dur, 4),
            "page": fn, "beats": beats, "bpm": bpm,
            "x0": round(x0 / lay["w"], 4), "x1": round(x1 / lay["w"], 4),
            "y0": round(max(0.0, lay["tops"][si] - pad) / lay["h"], 4),
            "y1": round(min(lay["h"], lay["bottoms"][si] + pad) / lay["h"], 4),
            # ataques reales: [posición en negras, x como fracción de la hoja]
            "hx": ([[round(b, 4), round(x / lay["w"], 4)] for b, x in bmap]
                   if bmap else None),
        })
    # Relación ancho/alto de la hoja (píxeles del PNG) → el editor puede
    # dimensionar el lienzo de la partitura antes de que la imagen decodifique,
    # evitando saltos de layout al cambiar de página.
    fn0 = engine.cfg["file_nums"][0]
    pw0, ph0 = engine.page_px.get(fn0, (1, 1.4142))
    return {
        "song_name":      engine.song_name,
        "score_bpm":      engine._timeline[0][5] if engine._timeline else 120,
        "lead_in":        round(lead, 4),
        "count_beats":    getattr(engine, "count_beats", 0),
        "music_duration": round(engine.total_duration - lead, 4),
        "pages":          list(engine.cfg["file_nums"]),
        "page_aspect":    round(pw0 / ph0, 5) if ph0 else 0.7071,
        "measures":       measures,
        "audio":          analysis,
    }


def _run_finalize(job_id, offset, stretch, skip_audio):
    """Espera el render en segundo plano y (si corresponde) mezcla el audio."""
    import time
    from audio_sync import run_mux
    from progress import Progress

    j = jobs[job_id]

    def prog(pct, msg):
        j["pct"] = pct
        j["msg"] = msg

    progress = j.get("progress") or Progress(web_cb=prog)
    progress.web_cb = prog
    progress.reset()

    try:
        r = j["render"]
        if not r["done"]:
            # La barra de consola del render la dibuja su propio hilo
            # (_render_silent); acá solo se refleja la espera en la web.
            while not r["done"]:
                prog(5 + int(r["pct"] * 0.75),
                     f"Renderizando el video… ({r['pct']}%)")
                time.sleep(0.3)
        if r["error"]:
            raise RuntimeError(r["error"])

        # Pulsos corregidos a mano en el editor ([P]): se aplican al motor y
        # el video se renderiza de nuevo con la línea en las posiciones
        # corregidas. Si esta misma tanda ya se aplicó (reintento), se salta.
        fixes = j.get("pulse_fixes") or []
        sig = tuple(sorted((f["i"], f["k"], f["x"]) for f in fixes))
        if fixes and sig != j.get("_fixes_applied"):
            n_ok = _apply_pulse_fixes(j["engine"], fixes)
            if n_ok:
                what = "pulsos corregidos" if n_ok != 1 else "pulso corregido"
                with progress.phase(f"Aplicando {n_ok} {what} (re-render)",
                                    span=(10, 78)) as ph:
                    r["path"] = _render_video_frames(
                        j, j["engine"], lambda p: ph.update(p / 100))
            j["_fixes_applied"] = sig

        if skip_audio:
            j["output"] = r["path"]
            prog(100, "¡Video listo para descargar!")
            j["done"] = True
            progress.announce("  ✔ Video (sin audio) listo para descargar.")
            return

        with progress.phase("Mezclando audio y video", span=(80, 99)) as ph:
            ph.update(0.1, "ffmpeg")
            final_path = os.path.join(j["workdir"], "scrolling_score_final.mp4")
            run_mux(
                _find_ffmpeg(),
                video_path=r["path"],
                audio_path=j["audio_path"],
                out_path=final_path,
                offset=offset,
                stretch=stretch,
                lead_in=j.get("lead_in", 0.0),
                video_duration=j["video_duration"],
            )
        j["output"] = final_path
        j["pct"]  = 100
        j["msg"]  = "¡Video final con audio listo para descargar!"
        j["done"] = True
        progress.announce("  ✔ Video final con audio listo para descargar.")
    except Exception as exc:
        progress.error(exc)
        j["pct"]   = 0
        j["msg"]   = f"Error: {exc}"
        j["error"] = str(exc)
        j["done"]  = True
    finally:
        j["finalizing"] = False


def _apply_pulse_fixes(engine, fixes):
    """Aplica al motor los pulsos corregidos a mano en el editor. Cada fix
    mueve la posición horizontal (x) de UN pulso guardado en beat_x_map; el
    playhead del video final la usa tal cual. Devuelve cuántos se aplicaron."""
    n_ok = 0
    for f in fixes:
        i, k, x = f["i"], f["k"], f["x"]
        if not (0 <= i < len(engine._timeline)):
            continue
        _t, _dur, fn, mi, _beats, _bpm = engine._timeline[i]
        bmap = engine.beat_x_map.get(fn, [])
        if not (0 <= mi < len(bmap)) or not bmap[mi] or not (0 <= k < len(bmap[mi])):
            continue
        b_pos = bmap[mi][k][0]
        bmap[mi][k] = (b_pos, x * engine.layouts[fn]["w"])
        n_ok += 1
    return n_ok


def _find_ffmpeg():
    # 1. Bundled in vendor/ (el .exe solo sirve en Windows)
    vendor_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    names = ["ffmpeg.exe"] if os.name == "nt" else ["ffmpeg"]
    for name in names:
        vendor = os.path.join(vendor_dir, name)
        if os.path.isfile(vendor):
            return vendor
    # 2. On PATH (works on Mac/Linux too)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "ffmpeg no encontrado. Colocá ffmpeg.exe en la carpeta vendor/ "
        "o agregalo al PATH del sistema."
    )

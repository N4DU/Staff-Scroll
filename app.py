import os
import re
import math
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

        _sweep_stale_workdirs()

        job_id = uuid.uuid4().hex[:10]
        job_dir = os.path.join(tempfile.gettempdir(), f"scrolling_score_{job_id}")
        mscz_dir = os.path.join(job_dir, "uploads")
        os.makedirs(mscz_dir, exist_ok=True)

        def _reject(msg):
            # si la validación falla a mitad de lote, no dejar basura en disco
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({"error": msg}), 400

        saved_paths = []
        for idx, f in enumerate(files):
            if not f.filename.lower().endswith(".mscz"):
                return _reject(f"Archivo no válido: {f.filename}")
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
                return _reject(f"Formato de audio no soportado: {ext}")
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
        data = request.get_json(silent=True) or {}
        job_id = data.get("job_id")
        options = data.get("options", {})

        if not job_id or job_id not in jobs:
            return jsonify({"error": "Trabajo no encontrado"}), 404

        job = jobs[job_id]
        with _jobs_lock:
            if job.get("started"):
                return jsonify({"error": "El trabajo ya está en proceso"}), 409
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
                    yield f"data: {json.dumps({'pct': 0, 'msg': 'Trabajo no encontrado', 'done': True, 'error': 'not_found'})}\n\n"
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
            return jsonify({"error": "Trabajo no encontrado"}), 404
        j = jobs[job_id]
        if not j["done"] or j["error"]:
            return jsonify({"error": "Video no disponible"}), 425
        out = j.get("output")
        if not out or not os.path.isfile(out):
            return jsonify({"error": "Archivo no encontrado"}), 404

        def _cleanup():
            # Margen amplio para que send_file termine de transmitir el video
            # antes de borrar el directorio de trabajo. Si el usuario pidió
            # OTRO finalize (ajustó la sincronización tras descargar), no se
            # borra nada: el barrido de /upload limpiará el directorio después.
            import time as _t
            _t.sleep(120)
            if j.get("finalizing") or not j.get("done"):
                j["_cleanup"] = False
                return
            shutil.rmtree(j["workdir"], ignore_errors=True)
            jobs.pop(job_id, None)

        with _jobs_lock:
            if not j.get("_cleanup"):        # un solo hilo de limpieza por job
                j["_cleanup"] = True
                threading.Thread(target=_cleanup, daemon=True).start()
        # el archivo se llama como la canción ("That Band.mp4"), no
        # "scrolling_score.mp4"; solo se quitan caracteres inválidos en
        # nombres de archivo (Windows es el más estricto)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", j.get("song_name") or "").strip()
        if not name or name == "Scrolling Score":
            name = "scrolling_score"
        return send_file(out, as_attachment=True, download_name=f"{name}.mp4",
                         mimetype="video/mp4")

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

    @app.route("/wavepeaks/<job_id>")
    def wavepeaks(job_id):
        """Envolvente binaria de alta resolución de la canción (int8:
        mín, máx, rms por ventana — ver audio_sync.waveform_envelope)."""
        j = jobs.get(job_id)
        if not j or not j.get("wave_env"):
            return jsonify({"error": "Envolvente no disponible"}), 404
        return Response(j["wave_env"], mimetype="application/octet-stream",
                        headers={"X-Envelope-Rate": str(j.get("wave_env_rate", 400)),
                                 "Cache-Control": "no-store"})

    @app.route("/editor_data/<job_id>")
    def editor_data(job_id):
        j = jobs.get(job_id)
        if not j or not j.get("editor_data"):
            return jsonify({"error": "Datos del editor no disponibles"}), 404
        return jsonify(j["editor_data"])

    @app.route("/finalize/<job_id>", methods=["POST"])
    def finalize(job_id):
        j = jobs.get(job_id)
        # "render" existe solo si el trabajo inicial llegó al editor: eso es lo
        # que habilita finalizar. Un error de un finalize ANTERIOR no bloquea
        # el reintento (j["error"] se limpia al arrancar de nuevo).
        if not j or not j.get("render"):
            return jsonify({"error": "El trabajo aún no está listo"}), 409
        data = request.get_json(silent=True) or {}
        skip = bool(data.get("skip"))
        try:
            # offset negativo = la partitura empieza ANTES que el audio
            offset  = max(-7200.0, min(7200.0, float(data.get("offset", 0.0))))
            stretch = max(0.5, min(2.0, float(data.get("stretch", 1.0))))
            # correcciones de pulsos hechas en el editor (modo D):
            # {i: índice de compás en la línea de tiempo, k: pulso, x: fracción}
            fixes = []
            for f in (data.get("pulse_fixes") or [])[:4000]:
                fixes.append({"i": int(f["i"]), "k": int(f["k"]),
                              "x": min(1.0, max(0.0, float(f["x"])))})
        except (TypeError, ValueError, KeyError):
            return jsonify({"error": "Parámetros inválidos"}), 400

        with _jobs_lock:
            if j.get("finalizing"):
                return jsonify({"error": "Ya se está generando el video final"}), 409
            j["finalizing"] = True
            # dentro del lock: que un doble-submit no pise las correcciones
            # del finalize que ya arrancó
            j["pulse_fixes"] = fixes
        j["done"] = False
        j["error"] = None
        j["editor"] = False
        j["pct"] = 5
        j["msg"] = "Preparando el video final…"

        threading.Thread(target=_run_finalize, args=(job_id, offset, stretch, skip),
                         daemon=True).start()
        return jsonify({"status": "iniciado"})

    return app


def _sweep_stale_workdirs(max_age_h=24):
    """Borra directorios scrolling_score_* huérfanos (de sesiones anteriores
    que nunca descargaron). Sin esto, cada trabajo abandonado deja su carpeta
    en %TEMP% para siempre."""
    import time as _t
    base = tempfile.gettempdir()
    try:
        names = os.listdir(base)
    except OSError:
        return
    now = _t.time()
    for n in names:
        if not n.startswith("scrolling_score_"):
            continue
        if n[len("scrolling_score_"):] in jobs:
            continue                     # trabajo vivo de esta sesión
        p = os.path.join(base, n)
        try:
            if now - os.path.getmtime(p) > max_age_h * 3600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


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
        # Validar las opciones ANTES del pipeline: un valor mal formado debe
        # fallar en milisegundos, no después de minutos de render de MuseScore.
        user_cfg = _sanitize_options(options)

        prog(2, "Iniciando el proceso…")
        workdir    = j["workdir"]
        mscz_paths = j["mscz_paths"]
        render_dir = os.path.join(workdir, "render")

        # Fases 1-2: pipeline de MuseScore (extraer + renderizar, 2% → 72%)
        engine_cfg = process_mscz_files(
            mscz_paths=mscz_paths,
            workdir=render_dir,
            progress=progress,
        )
        engine_cfg.update(user_cfg)

        with progress.phase("Construyendo motor de video", span=(72, 82)) as ph:
            engine = build_engine(engine_cfg, phase=ph)

        j["engine"]         = engine
        j["song_name"]      = getattr(engine, "song_name", "") or ""
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
            # la envolvente es binaria: se sirve por /wavepeaks, no en el JSON
            j["wave_env"]      = analysis.pop("envelope", b"")
            j["wave_env_rate"] = analysis.pop("envelope_rate", 0)
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
        j["started"] = False   # habilita reintentar /generate sin re-subir


def _sanitize_options(options):
    """Valida y normaliza las opciones del usuario → claves del motor.
    Lanza ValueError con mensaje claro si algo viene roto (se llama ANTES del
    pipeline pesado para fallar rápido)."""
    cfg = {}
    try:
        if options.get("show_header") is False:
            cfg["show_header"] = False
        if "page_gap_pct" in options:
            cfg["page_gap_pct"] = min(100.0, max(0.0, float(options["page_gap_pct"])))
        if "playhead_frac" in options:
            cfg["playhead_frac"] = min(1.0, max(0.0, float(options["playhead_frac"])))
        if "count_in_beats" in options:
            cfg["count_in_beats"] = max(0, min(16, int(options["count_in_beats"])))
        if options.get("song_name"):
            cfg["song_name"] = str(options["song_name"])

        # Línea de tiempo (playhead): estilo configurable
        if options.get("playhead_mode") in ("fluid", "beats"):
            cfg["playhead_mode"] = options["playhead_mode"]
        col = options.get("playhead_color")
        if isinstance(col, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", col):
            c = col.lstrip("#")
            cfg["playhead_color"] = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
        if "playhead_alpha" in options:
            cfg["playhead_alpha"] = min(1.0, max(0.05, float(options["playhead_alpha"])))
        if "playhead_w" in options:
            cfg["playhead_w"] = max(1, min(8, int(options["playhead_w"])))
        if options.get("show_playhead") is False:
            cfg["playhead_w"] = 0

        # Resolución del dispositivo destino (par: se exige para yuv420p)
        if options.get("video_w") and options.get("video_h"):
            w = max(320, min(3840, int(options["video_w"])))
            h = max(240, min(2160, int(options["video_h"])))
            cfg["video_w"] = w - (w % 2)
            cfg["video_h"] = h - (h % 2)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Opción inválida en la configuración: {exc}") from exc
    return cfg


class _RenderAborted(Exception):
    """El render se canceló a propósito (lo reemplaza otro con correcciones)."""


# Keyframe estricto cada _GOP_FRAMES (2 s a 30 fps) y sin B-frames: el video
# queda cortable EXACTAMENTE en esos límites. Al corregir pulsos, solo los
# tramos afectados se re-renderizan; el resto se copia sin re-codificar.
_GOP_FRAMES = 60


def _x264_args():
    # veryfast: ~2x más rápido que "fast" con diferencia de calidad
    # imperceptible a crf 18 (contenido de partitura: trazos nítidos)
    return ["-vcodec", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p",
            "-x264-params",
            f"keyint={_GOP_FRAMES}:min-keyint={_GOP_FRAMES}:scenecut=0:bframes=0"]


def _encode_frames(j, engine, f0, f1, out_path, report, should_abort=None):
    """Codifica los frames [f0, f1) del motor a `out_path` (video mudo).
    report(pct 0-100 del tramo). `should_abort()` (opcional): si devuelve
    True se corta ffmpeg, se borra el parcial y se lanza _RenderAborted."""
    fps      = j["fps"]
    n_frames = f1 - f0
    W, H     = engine.video_w, engine.video_h

    cmd = [
        _find_ffmpeg(), "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "bgr24",
        "-r", str(fps), "-i", "pipe:0",
    ] + _x264_args() + [out_path]
    # stderr va a un archivo: con stderr=PIPE sin lector, el buffer se llena
    # y ffmpeg + este proceso quedan bloqueados (deadlock).
    stderr_path = os.path.join(j["workdir"], "ffmpeg_stderr.log")
    # Render en PARALELO: numpy suelta el GIL en las operaciones grandes, así
    # que varios hilos rinden frames a la vez (~2.8x en 1080, ~4x en 4K,
    # salida byte-idéntica al serial — verificado). La ventana acotada evita
    # acumular frames en RAM; se escriben a ffmpeg en orden estricto.
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    n_workers = min(4, max(2, (os.cpu_count() or 2) - 1))
    window = n_workers * 3
    with open(stderr_path, "wb") as errf:
        pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
        try:
            engine.render_frame(f0 / fps)     # calienta cachés (hilo único)
            with ThreadPoolExecutor(n_workers) as ex:
                pending = deque(ex.submit(engine.render_frame, (f0 + i) / fps)
                                for i in range(min(window, n_frames)))
                submitted = len(pending)
                for i in range(n_frames):
                    if should_abort is not None and i % 30 == 0 and should_abort():
                        raise _RenderAborted()
                    frame = pending.popleft().result()
                    if submitted < n_frames:
                        pending.append(ex.submit(engine.render_frame,
                                                 (f0 + submitted) / fps))
                        submitted += 1
                    pipe.stdin.write(frame.tobytes())
                    if i % max(1, n_frames // 100) == 0:
                        report(int(i * 100 / n_frames))
            pipe.stdin.close()
        except BrokenPipeError:
            pass  # ffmpeg murió a mitad de camino; el returncode lo dirá
        except _RenderAborted:
            try:
                pipe.stdin.close()
            except OSError:
                pass
            pipe.terminate()
            pipe.wait()
            try:
                os.remove(out_path)
            except OSError:
                pass
            raise
        except Exception:
            # cualquier otro fallo (p. ej. en render_frame): NUNCA dejar un
            # ffmpeg huérfano esperando stdin para siempre
            try:
                pipe.kill()
            except OSError:
                pass
            pipe.wait()
            raise
        pipe.wait()

    if pipe.returncode != 0:
        with open(stderr_path, encoding="utf-8", errors="replace") as f:
            err = f.read()
        raise RuntimeError(f"ffmpeg falló:\n{err[-600:]}")
    return out_path


def _render_video_frames(j, engine, report, should_abort=None):
    """Renderiza el video mudo COMPLETO (frames → ffmpeg). report(pct 0-100)."""
    n_frames = max(1, int(engine.total_duration * j["fps"]))
    out_path = os.path.join(j["workdir"], "scrolling_score.mp4")
    return _encode_frames(j, engine, 0, n_frames, out_path, report, should_abort)


def _count_frames(path):
    """Cantidad de frames de video del archivo (ffprobe)."""
    ffprobe = os.path.join(os.path.dirname(_find_ffmpeg()), "ffprobe")
    if not os.path.isfile(ffprobe) and not shutil.which("ffprobe"):
        return None
    r = subprocess.run(
        [shutil.which("ffprobe") or ffprobe, "-v", "error",
         "-count_packets", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _render_with_fixes_segmented(j, engine, fixes, base_path, report):
    """Re-render QUIRÚRGICO tras corregir pulsos: solo se re-codifican los
    tramos (GOPs de 2 s) cuyos compases cambiaron; el resto del video base se
    copia sin re-codificar y todo se concatena. Con pocas correcciones esto
    tarda una fracción del re-render completo.

    Devuelve la ruta del video nuevo, o None si no conviene/no se puede
    (el llamador cae al re-render completo)."""
    fps = j["fps"]
    n_frames = max(1, int(engine.total_duration * fps))
    total_chunks = (n_frames + _GOP_FRAMES - 1) // _GOP_FRAMES
    if total_chunks < 4 or not os.path.isfile(base_path):
        return None
    if _count_frames(base_path) != n_frames:
        return None          # el base no coincide (otro fps/duración): completo

    # Compases FÍSICOS corregidos → todas sus apariciones en la línea de
    # tiempo (repeticiones incluidas) marcan sus GOPs como afectados.
    phys = set()
    for f in fixes:
        i = f["i"]
        if 0 <= i < len(engine._timeline):
            _t, _d, fn, mi, _b, _bpm = engine._timeline[i]
            phys.add((fn, mi))
    chunks = set()
    for (t0, dur, fn, mi, _b, _bpm) in engine._timeline:
        if (fn, mi) in phys:
            a = int(t0 * fps) // _GOP_FRAMES
            b = int(math.ceil((t0 + dur) * fps)) // _GOP_FRAMES
            chunks.update(range(max(0, a), min(b, total_chunks - 1) + 1))
    if not chunks or len(chunks) > 0.6 * total_chunks:
        return None          # afecta a casi todo: el completo es más simple

    # GOPs → segmentos alternados (copiar / re-renderizar), en frames
    segs = []
    for c in range(total_chunks):
        kind = "render" if c in chunks else "copy"
        f0 = c * _GOP_FRAMES
        f1 = min(n_frames, f0 + _GOP_FRAMES)
        if segs and segs[-1][0] == kind:
            segs[-1][2] = f1
        else:
            segs.append([kind, f0, f1])

    seg_dir = os.path.join(j["workdir"], "segs")
    shutil.rmtree(seg_dir, ignore_errors=True)
    os.makedirs(seg_dir)
    n_render = sum(f1 - f0 for k, f0, f1 in segs if k == "render")
    done = [0]
    files = []
    for idx, (kind, f0, f1) in enumerate(segs):
        path = os.path.join(seg_dir, f"s{idx:04d}.mp4")
        if kind == "copy":
            # cortes SIEMPRE en múltiplos del GOP (keyframes exactos, sin
            # B-frames): -ss cae en keyframe y -frames:v corta exacto
            r = subprocess.run(
                [_find_ffmpeg(), "-y", "-ss", f"{f0 / fps:.6f}", "-i", base_path,
                 "-frames:v", str(f1 - f0), "-c", "copy",
                 "-avoid_negative_ts", "make_zero", path],
                capture_output=True)
            if r.returncode != 0:
                return None
        else:
            _encode_frames(j, engine, f0, f1, path,
                           lambda p, a=f0, b=f1: report(
                               int((done[0] + (b - a) * p / 100) * 100 / n_render)))
            done[0] += f1 - f0
        # verificación dura: cada segmento con la cantidad EXACTA de frames
        if _count_frames(path) != f1 - f0:
            return None
        files.append(path)

    list_path = os.path.join(seg_dir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file '{p}'\n")
    out_path = os.path.join(j["workdir"], "scrolling_score_fixed.mp4")
    r = subprocess.run(
        [_find_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", list_path,
         "-c", "copy", out_path], capture_output=True)
    if r.returncode != 0 or _count_frames(out_path) != n_frames:
        return None
    # el resultado pasa a ser el nuevo video base (conserva la grilla de GOPs)
    os.replace(out_path, base_path)
    shutil.rmtree(seg_dir, ignore_errors=True)
    return base_path


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
            try:
                r["path"] = _render_video_frames(
                    j, j["engine"], _report,
                    should_abort=lambda: bool(r.get("cancel")))
            except _RenderAborted:
                # hay pulsos corregidos esperando: no tiene sentido terminar
                # este render para tirarlo — el finalize hace UNO solo
                r["aborted"] = True
                ph.cancel("lo reemplaza el render con tus correcciones")
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
            # id del compás FÍSICO: con repeticiones, el mismo compás aparece
            # varias veces en la línea de tiempo pero comparte los pulsos —
            # el editor espeja las correcciones entre esas apariciones
            "pid": f"{fn}|{mi}",
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
        # sin claves binarias (la envolvente viaja aparte, por /wavepeaks)
        "audio":          {k: v for k, v in analysis.items() if k != "envelope"},
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

        # Pulsos corregidos a mano en el editor: se aplican al motor y el
        # video se renderiza con la línea en las posiciones corregidas. Si el
        # render en 2º plano AÚN está corriendo, se cancela (terminarlo para
        # tirarlo sería duplicar minutos de trabajo) y se renderiza UNA vez.
        # Un reintento con la misma tanda de correcciones no re-renderiza.
        fixes = j.get("pulse_fixes") or []
        sig = tuple(sorted((f["i"], f["k"], f["x"]) for f in fixes))
        need_apply = bool(fixes) and sig != j.get("_fixes_applied")
        if need_apply and not r["done"]:
            r["cancel"] = True

        if not r["done"]:
            # La barra de consola del render la dibuja su propio hilo
            # (_render_silent); acá solo se refleja la espera en la web.
            t_wait0 = time.time()
            while not r["done"]:
                if time.time() - t_wait0 > 4 * 3600:
                    raise RuntimeError("El render de fondo no respondió en 4 "
                                       "horas — reinicia el programa y vuelve a intentarlo.")
                prog(5 + int(r["pct"] * 0.75),
                     f"Renderizando el video… ({r['pct']}%)")
                time.sleep(0.3)
        if r["error"]:
            raise RuntimeError(r["error"])

        n_ok = _apply_pulse_fixes(j["engine"], fixes) if need_apply else 0
        if need_apply:
            j["_fixes_applied"] = sig
        if n_ok or r.get("aborted") or not r.get("path"):
            what = (f"Aplicando {n_ok} pulso{'s' if n_ok != 1 else ''} "
                    f"corregido{'s' if n_ok != 1 else ''}" if n_ok
                    else "Renderizando el video")
            with progress.phase(what, span=(10, 78)) as ph:
                new_path = None
                if n_ok and r.get("path") and not r.get("aborted"):
                    # re-render QUIRÚRGICO: solo los tramos afectados; el
                    # resto se copia del video base sin re-codificar
                    ph.update(0, "solo los tramos corregidos")
                    new_path = _render_with_fixes_segmented(
                        j, j["engine"], fixes, r["path"],
                        lambda p: ph.update(p / 100))
                if new_path is None:
                    new_path = _render_video_frames(
                        j, j["engine"], lambda p: ph.update(p / 100))
                r["path"] = new_path
            r["cancel"] = False
            r["aborted"] = False

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
        "ffmpeg no encontrado. Coloca ffmpeg.exe en la carpeta vendor/ "
        "o agrégalo al PATH del sistema."
    )

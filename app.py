import os
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

    @app.route("/video/<job_id>")
    def preview_video(job_id):
        j = jobs.get(job_id)
        if not j or not j.get("video_path") or not os.path.isfile(j["video_path"]):
            return jsonify({"error": "Video no disponible"}), 404
        # conditional=True habilita peticiones Range → el <video> puede hacer
        # seek sin descargar el archivo entero.
        return send_file(j["video_path"], mimetype="video/mp4", conditional=True)

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
        if not j or not j.get("done") or j.get("error"):
            return jsonify({"error": "Job no está listo"}), 409
        if not j.get("audio_path"):
            return jsonify({"error": "Este job no tiene audio"}), 400
        data = request.get_json(force=True)
        try:
            offset  = max(-3600.0, min(3600.0, float(data.get("offset", 0.0))))
            stretch = max(0.5, min(2.0, float(data.get("stretch", 1.0))))
        except (TypeError, ValueError):
            return jsonify({"error": "Parámetros inválidos"}), 400

        with _jobs_lock:
            if j.get("finalizing"):
                return jsonify({"error": "Ya se está generando el video final"}), 409
            j["finalizing"] = True
        j["done"] = False
        j["error"] = None
        j["editor"] = False
        j["pct"] = 5
        j["msg"] = "Preparando mezcla de audio…"

        threading.Thread(target=_run_finalize, args=(job_id, offset, stretch),
                         daemon=True).start()
        return jsonify({"status": "iniciado"})

    return app


# ─── job runner ──────────────────────────────────────────────────────────────

def _run_job(job_id, options):
    from musescore_pipeline import process_mscz_files
    from score_engine import build_engine

    j = jobs[job_id]

    def prog(pct, msg):
        j["pct"] = pct
        j["msg"] = msg

    try:
        prog(2, "Iniciando pipeline…")
        workdir    = j["workdir"]
        mscz_paths = j["mscz_paths"]
        render_dir = os.path.join(workdir, "render")

        # Phase 1: MuseScore pipeline (2% → 80%)
        engine_cfg = process_mscz_files(
            mscz_paths=mscz_paths,
            workdir=render_dir,
            progress_cb=prog,
        )

        # Merge user options into engine config
        if options.get("show_header") is False:
            engine_cfg["show_header"] = False
        if options.get("show_playhead") is False:
            engine_cfg["playhead_w"] = 0
        if "page_gap_pct" in options:
            engine_cfg["page_gap_pct"] = min(100.0, max(0.0, float(options["page_gap_pct"])))
        if "playhead_frac" in options:
            engine_cfg["playhead_frac"] = min(1.0, max(0.0, float(options["playhead_frac"])))
        if "count_in_beats" in options:
            engine_cfg["count_in_beats"] = max(0, min(16, int(options["count_in_beats"])))
        if options.get("song_name"):
            engine_cfg["song_name"] = str(options["song_name"])

        prog(81, "Construyendo motor de video…")
        engine = build_engine(engine_cfg)

        fps      = engine_cfg.get("fps", 30)
        n_frames = max(1, int(engine.total_duration * fps))
        W, H     = engine.video_w, engine.video_h
        out_path = os.path.join(workdir, "scrolling_score.mp4")

        ffmpeg_bin = _find_ffmpeg()

        cmd = [
            ffmpeg_bin, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{W}x{H}", "-pix_fmt", "bgr24",
            "-r", str(fps), "-i", "pipe:0",
            "-vcodec", "libx264", "-preset", "fast",
            "-crf", "18", "-pix_fmt", "yuv420p",
            out_path,
        ]

        prog(82, f"Iniciando renderizado ({n_frames} frames, ~{int(engine.total_duration)}s de video)…")
        # stderr va a un archivo: con stderr=PIPE sin lector, el buffer se
        # llena y ffmpeg + este proceso quedan bloqueados (deadlock).
        stderr_path = os.path.join(workdir, "ffmpeg_stderr.log")
        with open(stderr_path, "wb") as errf:
            pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
            try:
                for i in range(n_frames):
                    frame = engine.render_frame(i / fps)
                    pipe.stdin.write(frame.tobytes())
                    if i % max(1, n_frames // 50) == 0:
                        pct = 83 + int((i / n_frames) * 16)
                        prog(pct, f"Renderizando frame {i}/{n_frames}…")
                pipe.stdin.close()
            except BrokenPipeError:
                pass  # ffmpeg murió a mitad de camino; el returncode lo dirá
            pipe.wait()

        if pipe.returncode != 0:
            with open(stderr_path, encoding="utf-8", errors="replace") as f:
                err = f.read()
            raise RuntimeError(f"ffmpeg falló:\n{err[-600:]}")

        j["output"]         = out_path
        j["video_path"]     = out_path
        j["video_duration"] = engine.total_duration
        j["lead_in"]        = getattr(engine, "lead_in", 0.0)

        # Si hay audio: analizarlo y preparar los datos del editor de sync.
        if j.get("audio_path"):
            prog(99, "Analizando el audio (forma de onda y golpes)…")
            from audio_sync import analyze_audio
            analysis = analyze_audio(ffmpeg_bin, j["audio_path"])
            j["editor_data"] = {
                "song_name":      engine.song_name,
                "lead_in":        j["lead_in"],
                "video_duration": engine.total_duration,
                "score_bpm":      engine._timeline[0][5] if engine._timeline else 120,
                # Un registro por compás reproducido: tiempo en el video,
                # duración, página, índice de compás, pulsos y BPM. Esta es la
                # ventaja del editor: conoce la música compás a compás.
                "measures": [
                    {"t": round(t, 4), "dur": round(dur, 4), "page": fn,
                     "idx": mi, "beats": beats, "bpm": bpm}
                    for (t, dur, fn, mi, beats, bpm) in engine._timeline
                ],
                "audio": analysis,
            }
            j["editor"] = True
            j["pct"]  = 100
            j["msg"]  = "Partitura lista — abriendo editor de sincronización…"
            j["done"] = True
            return

        j["pct"]    = 100
        j["msg"]    = "¡Video listo para descargar!"
        j["done"]   = True

    except Exception as exc:
        j["pct"]   = 0
        j["msg"]   = f"Error: {exc}"
        j["error"] = str(exc)
        j["done"]  = True


def _run_finalize(job_id, offset, stretch):
    """Mezcla el video mudo con el audio según la alineación del editor."""
    from audio_sync import run_mux

    j = jobs[job_id]

    def prog(pct, msg):
        j["pct"] = pct
        j["msg"] = msg

    try:
        prog(10, "Mezclando audio y video…")
        final_path = os.path.join(j["workdir"], "scrolling_score_final.mp4")
        run_mux(
            _find_ffmpeg(),
            video_path=j["video_path"],
            audio_path=j["audio_path"],
            out_path=final_path,
            offset=offset,
            stretch=stretch,
            lead_in=j.get("lead_in", 0.0),
            video_duration=j["video_duration"],
            progress_cb=prog,
        )
        j["output"] = final_path
        j["pct"]  = 100
        j["msg"]  = "¡Video final con audio listo para descargar!"
        j["done"] = True
    except Exception as exc:
        j["pct"]   = 0
        j["msg"]   = f"Error: {exc}"
        j["error"] = str(exc)
        j["done"]  = True
    finally:
        j["finalizing"] = False


def _find_ffmpeg():
    # 1. Bundled in vendor/
    vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "ffmpeg.exe")
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

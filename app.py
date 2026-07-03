import os
import uuid
import shutil
import threading
import subprocess
import tempfile
from flask import Flask, request, jsonify, render_template, Response, send_file


jobs = {}


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
        for f in files:
            if not f.filename.lower().endswith(".mscz"):
                return jsonify({"error": f"Archivo no válido: {f.filename}"}), 400
            dest = os.path.join(mscz_dir, f.filename)
            f.save(dest)
            saved_paths.append(dest)

        jobs[job_id] = {
            "pct": 0,
            "msg": "Archivos subidos correctamente",
            "done": False,
            "error": None,
            "output": None,
            "mscz_paths": saved_paths,
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
                payload = {"pct": j["pct"], "msg": j["msg"], "done": j["done"], "error": j["error"]}
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
            import time as _t
            _t.sleep(10)
            shutil.rmtree(j["workdir"], ignore_errors=True)
            jobs.pop(job_id, None)

        threading.Thread(target=_cleanup, daemon=True).start()
        return send_file(out, as_attachment=True, download_name="scrolling_score.mp4", mimetype="video/mp4")

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
        if "page_gap_extra_px" in options:
            engine_cfg["page_gap_extra_px"] = int(options["page_gap_extra_px"])
        if "playhead_frac" in options:
            engine_cfg["playhead_frac"] = float(options["playhead_frac"])
        if options.get("song_name"):
            engine_cfg["song_name"] = str(options["song_name"])

        prog(81, "Construyendo motor de video…")
        engine = build_engine(engine_cfg)

        fps      = engine_cfg.get("fps", 30)
        n_frames = int(engine.total_duration * fps)
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
        pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

        for i in range(n_frames):
            frame = engine.render_frame(i / fps)
            pipe.stdin.write(frame.tobytes())
            if i % max(1, n_frames // 50) == 0:
                pct = 83 + int((i / n_frames) * 16)
                prog(pct, f"Renderizando frame {i}/{n_frames}…")

        pipe.stdin.close()
        _, stderr_out = pipe.communicate()

        if pipe.returncode != 0:
            err = stderr_out.decode("utf-8", errors="replace")
            raise RuntimeError(f"ffmpeg falló:\n{err[-600:]}")

        j["output"] = out_path
        j["pct"]    = 100
        j["msg"]    = "¡Video listo para descargar!"
        j["done"]   = True

    except Exception as exc:
        j["pct"]   = 0
        j["msg"]   = f"Error: {exc}"
        j["error"] = str(exc)
        j["done"]  = True


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

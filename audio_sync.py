"""audio_sync.py — soporte de audio para el editor de sincronización.

Tres responsabilidades, todas apoyadas en ffmpeg (ya requerido por la app)
y numpy (ya requerido por el motor) — sin dependencias nuevas:

  1. Decodificar cualquier formato de audio a PCM mono y extraer:
       - la forma de onda reducida (picos min/max por bloque) para dibujarla
         en el editor,
       - los "onsets" (golpes/transitorios) por flujo espectral, que en una
         mezcla completa suelen coincidir con bombo/tarola — se muestran como
         guías visuales y para snapping, nunca como decisión automática.
  2. Construir el comando ffmpeg que mezcla el video mudo con el audio según
     la alineación elegida en el editor (offset + factor de tempo).
  3. Ejecutar esa mezcla reportando progreso.

Modelo de alineación (el mismo que usa el editor en JS):
    audio_t = offset + k * score_t
  donde score_t=0 es el instante en que arranca la partitura DENTRO del video
  (después del conteo previo), `offset` es en qué segundo del audio cae ese
  instante y `k` es la relación de tempo (k>1 → la grabación real es más
  lenta que la partitura). El video se retima con setpts (el scroll es
  movimiento continuo: retimarlo unos % es invisible); el audio nunca se
  estira, así la canción conserva su sonido exacto.
"""
import os
import subprocess
import numpy as np

_SR = 22050          # tasa de muestreo del análisis (mono)
_N_BUCKETS = 1600    # resolución de la forma de onda enviada al editor
_FFT = 1024
_HOP = 512


# ─── decodificación ──────────────────────────────────────────────────────────

def decode_audio(ffmpeg_bin, audio_path, sr=_SR):
    """Decodifica cualquier audio a PCM mono float32 en [-1, 1]."""
    cmd = [ffmpeg_bin, "-v", "error", "-i", audio_path,
           "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(sr),
           "pipe:1"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or len(r.stdout) < 2:
        err = r.stderr.decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(f"No se pudo decodificar el audio:\n{err}")
    samples = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sr


# ─── análisis para el editor ─────────────────────────────────────────────────

def waveform_peaks(samples, n_buckets=_N_BUCKETS):
    """Picos min/max por bloque, para dibujar la forma de onda completa."""
    n = len(samples)
    if n == 0:
        return {"mins": [], "maxs": []}
    bucket = max(1, n // n_buckets)
    usable = (n // bucket) * bucket
    chunks = samples[:usable].reshape(-1, bucket)
    return {"mins": [round(float(v), 3) for v in chunks.min(axis=1)],
            "maxs": [round(float(v), 3) for v in chunks.max(axis=1)]}


def detect_onsets(samples, sr=_SR):
    """Onsets por flujo espectral con umbral adaptativo (mediana local).

    Devuelve tiempos en segundos. Pensado como guía visual/snapping: en una
    mezcla completa los picos más marcados son casi siempre bombo y tarola.
    """
    n = len(samples)
    if n < _FFT * 4:
        return []
    n_frames = 1 + (n - _FFT) // _HOP
    idx = np.arange(_FFT)[None, :] + _HOP * np.arange(n_frames)[:, None]
    frames = samples[idx] * np.hanning(_FFT)[None, :]
    mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    flux = np.maximum(mag[1:] - mag[:-1], 0.0).sum(axis=1)
    if flux.max() <= 0:
        return []
    flux /= flux.max()

    # Umbral adaptativo: mediana en ventana deslizante (~0.5 s)
    win = 21
    padded = np.pad(flux, win // 2, mode="edge")
    med = np.median(np.lib.stride_tricks.sliding_window_view(padded, win), axis=1)
    thr = med * 1.5 + 0.02

    min_gap = int(0.09 * sr / _HOP)  # separación mínima de 90 ms entre onsets
    onsets, last = [], -min_gap
    for i in range(1, len(flux) - 1):
        if flux[i] > thr[i] and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1]:
            if i - last >= min_gap:
                onsets.append(round((i + 1) * _HOP / sr, 4))
                last = i
    return onsets


def analyze_audio(ffmpeg_bin, audio_path):
    """Análisis completo para el editor: duración, forma de onda y onsets."""
    samples, sr = decode_audio(ffmpeg_bin, audio_path)
    return {
        "duration": round(len(samples) / sr, 3),
        "waveform": waveform_peaks(samples),
        "onsets":   detect_onsets(samples, sr),
    }


# ─── mezcla final ────────────────────────────────────────────────────────────

def build_mux_command(ffmpeg_bin, video_path, audio_path, out_path,
                      offset, stretch, lead_in, video_duration,
                      fade_out=1.5, fade_in=0.25):
    """Arma el comando ffmpeg que produce el video final con audio alineado.

    offset:  segundo del audio donde cae el inicio de la partitura (score_t=0)
    stretch: k = segundos-de-audio por segundo-de-partitura (1.0 = tempos iguales)
    lead_in: segundos de conteo previo al inicio de la partitura en el video
    video_duration: duración del video mudo (sin retimar)
    """
    k = float(stretch)
    if not (0.5 <= k <= 2.0):
        raise ValueError(f"Factor de tempo fuera de rango razonable: {k}")
    total = video_duration * k                # duración final del video retimado
    s0 = float(offset) - lead_in * k          # posición del audio en T=0 final

    achain = []
    if s0 >= 0:
        achain.append(f"atrim=start={s0:.4f}")
        achain.append("asetpts=PTS-STARTPTS")
        if s0 > 0.01 and fade_in > 0:
            achain.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    else:
        delay_ms = int(round(-s0 * 1000))
        achain.append(f"adelay={delay_ms}:all=1")
    if fade_out > 0 and total > fade_out:
        achain.append(f"afade=t=out:st={max(0.0, total - fade_out):.4f}:d={fade_out:.3f}")
    afilter = "[1:a]" + ",".join(achain) + "[a]"

    cmd = [ffmpeg_bin, "-y", "-i", video_path, "-i", audio_path]
    retime = abs(k - 1.0) > 0.0005
    if retime:
        cmd += ["-filter_complex", f"[0:v]setpts={k:.6f}*PTS[v];{afilter}",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-r", "30"]
    else:
        cmd += ["-filter_complex", afilter,
                "-map", "0:v", "-map", "[a]", "-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-t", f"{total:.4f}", out_path]
    return cmd


def run_mux(ffmpeg_bin, video_path, audio_path, out_path,
            offset, stretch, lead_in, video_duration, progress_cb=None):
    """Ejecuta la mezcla. stderr a archivo (mismo criterio anti-deadlock que
    el render principal)."""
    cmd = build_mux_command(ffmpeg_bin, video_path, audio_path, out_path,
                            offset, stretch, lead_in, video_duration)
    if progress_cb:
        progress_cb(50, "Mezclando audio y video…")
    stderr_path = os.path.join(os.path.dirname(out_path), "ffmpeg_mux_stderr.log")
    with open(stderr_path, "wb") as errf:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=errf)
    if r.returncode != 0:
        with open(stderr_path, encoding="utf-8", errors="replace") as f:
            err = f.read()
        raise RuntimeError(f"ffmpeg falló mezclando el audio:\n{err[-600:]}")
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("La mezcla no produjo un archivo de salida válido")
    return out_path

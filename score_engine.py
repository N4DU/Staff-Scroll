"""score_engine.py — motor de renderizado del video de partitura con scroll.

v6 — cambios principales respecto a v5:
  * "Brecha entre páginas" (page_gap_pct, 0–100 %) ahora controla el PUNTO DE
    RECORTE de cada página en las uniones:
      0 %   → se recorta exactamente al borde del pentagrama (máxima
              continuidad; puede cortar símbolos que sobresalen).
      100 % → no se recorta nada: se conserva el final completo de la hoja
              anterior y el comienzo completo de la siguiente.
    Las páginas recortadas se apilan una a continuación de la otra; el scroll
    y las velocidades se recalculan a partir de las posiciones reales, por lo
    que la sincronización musical se mantiene exacta en cada compás.
  * El tempo se lee de la partitura (elemento <Tempo>) y se hereda entre
    páginas; los cambios de tempo y de compás a mitad de obra se respetan
    compás a compás.
  * En los saltos de repetición hacia atrás, el scroll sigue avanzando con la
    línea actual hasta el 70 % del último compás y recién entonces vuelve
    atrás, para no perder la lectura mientras todavía se está tocando.
"""
import os, re, math, bisect
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import xml.etree.ElementTree as ET

# Los PNG que abrimos son SIEMPRE archivos locales renderizados por MuseScore
# (no contenido de internet), así que el límite anti "decompression bomb" de
# PIL solo genera advertencias alarmantes en la consola. Se sube el límite en
# vez de desactivarlo: si una imagen supera esto, algo está realmente mal.
Image.MAX_IMAGE_PIXELS = 300_000_000

DEFAULT_CONFIG = {
    "bpm":             120,    # tempo de reserva si la partitura no lo indica
    "fps":             30,
    "video_w":         1080,
    "n_visible_lines": 4,
    # Posición de la línea lectora: 0.0=arriba  0.5=centro  1.0=abajo
    "playhead_frac":   0.5,
    # Brecha entre páginas: 0 = recorte al borde del pentagrama,
    # 100 = se conserva la hoja completa (nunca se cortan símbolos).
    "page_gap_pct":    30,
    "score_bg":        (255, 255, 255),
    "bg":              (255, 255, 255),  # fondo del lienzo = blanco → uniones limpias
    "playhead_color":  (255, 155, 0),
    "playhead_w":      3,
    # Movimiento de la línea: "fluid" = continuo, "beats" = salta de a tiempos
    "playhead_mode":   "fluid",
    "playhead_alpha":  1.0,     # 1.0 = opaca … 0.05 = casi invisible
    # Resolución de salida: video_h=None → se calcula de n_visible_lines
    "video_h":         None,
    "song_name":       "",     # se extrae de la partitura si está vacío
    "show_header":     True,
    # Conteo previo: cantidad de pulsos (al tempo del primer compás) que se
    # muestran antes de que arranque el scroll. 0 = sin conteo.
    "count_in_beats":  0,
    # Rutas — se completan por trabajo, no confiar en los valores por defecto
    "mscx_dir":  None,
    "png_dir":   None,
    "svg_dir":   None,
    "file_nums": None,
    "name_tpl":  "{i}-score",
}

# Padding (en unidades SVG) que se conserva sobre la primera página y bajo la
# última — esos bordes no son uniones entre páginas, así que no dependen de la
# brecha configurada.
_PAD_SVG = 120

_HEADER_H = 38      # altura de la barra de encabezado, px
_FADE_DUR = 0.8     # duración del fundido del encabezado, s

# ─── helpers ─────────────────────────────────────────────────────────────────

def _beat_count(beats):
    """Cantidad de pulsos visibles de un compás. Con una cantidad entera de
    negras usa esa cantidad (4/4 → 4); con métricas fraccionarias (5/8 → 2.5
    negras) baja a la grilla de corcheas (5 pulsos) — redondear 2.5 a 2
    desincronizaba el modo 'de a saltitos' y los puntos de pulso."""
    nb = int(round(beats))
    if abs(beats - nb) > 1e-6:
        nb = int(round(beats * 2))
    return max(1, nb)


def _compose_rgba(img_rgba, bg=(255, 255, 255)):
    arr = np.array(img_rgba, dtype=np.float32)
    rgb, a = arr[:, :, :3], arr[:, :, 3:4] / 255.0
    return (rgb * a + np.array(bg, dtype=np.float32) * (1 - a)).astype(np.uint8)


def _parse_svg_layout(svg_path):
    with open(svg_path) as f:
        content = f.read()
    # MuseScore 3 exporta "WIDTHpx"; MuseScore 4 exporta "WIDTHmm" — en ese
    # caso usamos el viewBox, que está en las mismas unidades que las coordenadas.
    h_m = re.search(r'height="([\d.]+)px"', content)
    w_m = re.search(r'width="([\d.]+)px"', content)
    if h_m and w_m:
        h, w = float(h_m.group(1)), float(w_m.group(1))
    else:
        vb = re.search(r'viewBox="[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)"', content)
        if not vb:
            raise ValueError(f"SVG sin dimensiones reconocibles: {os.path.basename(svg_path)}")
        w, h = float(vb.group(1)), float(vb.group(2))
    pts_list = re.findall(r'polyline class="StaffLines"[^>]*points="([^"]+)"', content)
    x_vals, y_set = [], set()
    line_ext = {}  # y de cada línea de pentagrama → (x_min, x_max)
    for pts in pts_list:
        xs_l, ys_l = [], []
        for coord in pts.strip().split():
            x, y = coord.split(',')
            xs_l.append(float(x))
            ys_l.append(float(y))
        x_vals.extend(xs_l)
        for y in ys_l:
            y_set.add(y)
            lo, hi = line_ext.get(y, (min(xs_l), max(xs_l)))
            line_ext[y] = (min(lo, min(xs_l)), max(hi, max(xs_l)))
    if not y_set:
        raise ValueError(
            f"No se encontraron líneas de pentagrama en {os.path.basename(svg_path)} "
            "— ¿el archivo exportado es realmente una partitura?")
    y_sorted = sorted(y_set)
    # Umbral adaptativo: funciona con MuseScore 3 (px) y MuseScore 4 (unidades
    # de viewBox). Dentro de un sistema las líneas están muy juntas; entre
    # sistemas la separación es mucho mayor. La mediana de las diferencias es
    # siempre una separación intra-sistema, así que 1.5× es un corte seguro.
    diffs = [y_sorted[i + 1] - y_sorted[i] for i in range(len(y_sorted) - 1)]
    median_diff = sorted(diffs)[len(diffs) // 2] if diffs else 60
    threshold = max(60, median_diff * 1.5)
    systems, cur = [], [y_sorted[0]]
    for y in y_sorted[1:]:
        if y - cur[-1] < threshold:
            cur.append(y)
        else:
            systems.append(cur)
            cur = [y]
    systems.append(cur)
    # Pentagramas de UNA línea (percusión: cencerro, pandereta…): el grupo
    # tiene una sola y → altura cero, lo que rompe todos los cálculos de
    # bandas. Se les da una banda sintética de ±1.5 espacios de línea,
    # recortada para no invadir a los sistemas vecinos.
    raw = [(min(s), max(s)) for s in systems]
    tops, bottoms = [], []
    for i, (t, b) in enumerate(raw):
        if b - t < 1e-6:
            pad = 1.5 * min(40.0, median_diff)
            lo = raw[i - 1][1] + 2 if i > 0 else 0.0
            hi = raw[i + 1][0] - 2 if i + 1 < len(raw) else h
            t2, b2 = max(lo, t - pad), min(hi, b + pad)
            t, b = (t2, b2) if b2 - t2 > 1 else (t - 1, b + 1)
        tops.append(t)
        bottoms.append(b)

    # Extremos horizontales POR SISTEMA (un primer sistema con sangría no
    # debe heredar el ancho de los demás).
    lefts, rights = [], []
    for s in systems:
        l = min(line_ext[y][0] for y in s)
        r = max(line_ext[y][1] for y in s)
        lefts.append(l)
        rights.append(r)

    # ── Barras de compás: posiciones exactas de cada compás ─────────────────
    # MuseScore exporta cada barline como polyline vertical. Con ellas el
    # playhead deja de suponer compases de ancho uniforme (MuseScore los
    # dibuja más anchos o angostos según la densidad de notas).
    bar_by_sys = [[] for _ in systems]
    for pts in re.findall(r'polyline class="BarLine"[^>]*points="([^"]+)"', content):
        xs_b, ys_b = [], []
        for coord in pts.strip().split():
            x, y = coord.split(',')
            xs_b.append(float(x))
            ys_b.append(float(y))
        bx = sum(xs_b) / len(xs_b)
        by = (min(ys_b) + max(ys_b)) / 2
        best, best_d = None, None
        for si in range(len(systems)):
            if tops[si] - 10 <= by <= bottoms[si] + 10:
                best = si
                break
            d = min(abs(by - tops[si]), abs(by - bottoms[si]))
            if best_d is None or d < best_d:
                best, best_d = si, d
        bar_by_sys[best].append(bx)

    # separación entre líneas del pentagrama: la escala de la página, usada
    # por todos los umbrales geométricos
    line_gap = (bottoms[0] - tops[0]) / 4.0 if bottoms[0] > tops[0] else 15.0

    # Límites de compás por sistema: [x0, x1, …] (len = compases + 1).
    # Las barras dobles/de repetición son pares cercanos (≈0.7 líneas) → se
    # fusionan con umbral proporcional. Una barra pegada al inicio del
    # sistema es la barra de repetición inicial dibujada tras la clave: NO es
    # un límite de compás (crearía un compás fantasma en la zona de la clave).
    sys_bounds = []
    for si, xs_b in enumerate(bar_by_sys):
        if not xs_b:
            sys_bounds = None
            break
        merged = []
        for x in sorted(xs_b):
            if merged and x - merged[-1] < line_gap * 0.9:
                merged[-1] = (merged[-1] + x) / 2
            else:
                merged.append(x)
        bounds = [lefts[si]] + [x for x in merged if x > lefts[si] + 8 * line_gap]
        if bounds[-1] < rights[si] - line_gap:
            bounds.append(rights[si])
        sys_bounds.append(bounds)

    # ── Anclas de notas y silencios ──────────────────────────────────────────
    # Cada cabeza de nota / silencio del SVG da un punto (x, y): el CENTRO
    # de su caja (el primer punto del trazo puede caer en cualquier borde del
    # glifo). Emparejados con los ataques del XML, permiten que el playhead
    # caiga en la posición GRABADA de cada golpe (MuseScore no reparte los
    # tiempos uniformemente: un pulso con semicorcheas ocupa más ancho que
    # uno con silencio).
    note_pts = []
    for tag_m in re.finditer(r'<[^>]*class="(?:Note|Rest)"[^>]*>', content):
        tag = tag_m.group(0)
        geo = re.search(r'\b(?:d|points)="([^"]+)"', tag)
        if not geo:
            continue
        nums = re.findall(r'-?\d+\.?\d*', geo.group(1))
        if len(nums) < 2:
            continue
        xs_g = [float(v) for v in nums[0::2]]
        ys_g = [float(v) for v in nums[1::2]]
        cx = (min(xs_g) + max(xs_g)) / 2
        cy = (min(ys_g) + max(ys_g)) / 2
        # Algunas versiones de MuseScore exportan la nota con coordenadas
        # LOCALES (alrededor del origen) y la posición real en un
        # transform="matrix(...)"/"translate(...)" — sin aplicarlo, todas las
        # notas caían en (≈0,≈0) y el mapa de pulsos quedaba vacío.
        tr = re.search(r'transform="matrix\(([^)]+)\)"', tag)
        if tr:
            try:
                a, b, c, d, e, f = [float(v) for v in
                                    re.split(r"[,\s]+", tr.group(1).strip())]
                cx, cy = a * cx + c * cy + e, b * cx + d * cy + f
            except ValueError:
                continue
        else:
            tr = re.search(r'transform="translate\(([^)]+)\)"', tag)
            if tr:
                try:
                    parts = [float(v) for v in
                             re.split(r"[,\s]+", tr.group(1).strip())]
                    cx += parts[0]
                    cy += parts[1] if len(parts) > 1 else 0.0
                except ValueError:
                    continue
        note_pts.append((cx, cy))


    return {"w": w, "h": h,
            "tops":    tops,
            "bottoms": bottoms,
            "lefts":   lefts,
            "rights":  rights,
            "sys_bounds": sys_bounds,   # None si el SVG no trae barlines
            "note_pts": note_pts,
            "line_gap": line_gap,
            "left_x":  min(x_vals) if x_vals else 0,
            "right_x": max(x_vals) if x_vals else w}


# Duración en negras de cada durationType de MuseScore
_DUR_BEATS = {"longa": 16, "breve": 8, "whole": 4, "half": 2, "quarter": 1,
              "eighth": 0.5, "16th": 0.25, "32nd": 0.125, "64th": 0.0625,
              "128th": 0.03125}


def _is_grace(chord_el):
    """Apoyaturas/acciaccaturas: se dibujan pero no ocupan tiempo."""
    for sub in chord_el:
        if sub.tag in ("acciaccatura", "appoggiatura") or sub.tag.startswith("grace"):
            return True
    return False


def _voice_onsets(voice_el, measure_beats):
    """Instantes de ataque (en negras desde el inicio del compás) de una voz.

    Devuelve (onsets, duración_total) o None si la voz no se puede
    interpretar con seguridad (tuplets, duraciones desconocidas, o se pasa
    del largo del compás). Las voces PUEDEN ser más cortas que el compás
    (MuseScore lo permite) y pueden saltar tiempo con <location>.
    """
    if voice_el.find('.//Tuplet') is not None:
        return None
    t, onsets = 0.0, []
    for ch in voice_el:
        if ch.tag == "location":
            # salto de cursor sin contenido: <location><fractions>1/4</fractions>
            fr = ch.find("fractions")
            if fr is None or not fr.text or "/" not in fr.text:
                return None
            try:
                num, den = fr.text.split("/")
                t += int(num) * 4.0 / int(den)
            except (ValueError, ZeroDivisionError):
                return None
            continue
        if ch.tag not in ("Chord", "Rest"):
            continue
        if ch.tag == "Chord" and _is_grace(ch):
            continue                 # no ocupa tiempo
        dt_el = ch.find("durationType")
        if dt_el is None or not dt_el.text:
            return None
        dt = dt_el.text.strip()
        if dt == "measure":          # silencio de compás entero
            beats = measure_beats
        else:
            beats = _DUR_BEATS.get(dt)
            if beats is None:
                return None
            dots = ch.find("dots")
            nd = int(dots.text) if dots is not None and dots.text else 0
            beats *= (2.0 - 0.5 ** nd)
        onsets.append(round(t, 4))
        t += beats
    if t > measure_beats + 1e-4:
        return None
    return onsets, t


def _parse_score_xml(mscx_path):
    """Lee compases, repeticiones, cambios de compás y de tempo de un .mscx.

    Devuelve:
      measures: lista por compás de {"beats": negras por compás, "qps": tempo
                en negras/segundo o None si el compás no trae marca de tempo,
                "onsets": instantes de ataque en negras (o None si no se pudo
                interpretar el ritmo con seguridad)}
      played:   índices de compás en orden de reproducción (repeticiones expandidas)
    """
    with open(mscx_path, encoding="utf-8") as f:
        root = ET.fromstring(f.read())
    # Solo el primer pentagrama: en partituras multi-staff cada Staff repite
    # los mismos compases y contarlos todos duplicaría la duración.
    staff = root.find('.//Score/Staff')
    if staff is None:
        staff = root.find('.//Staff')
    measures = staff.findall('Measure') if staff is not None else root.findall('.//Measure')
    if not measures:
        raise ValueError(f"No se encontraron compases en {os.path.basename(mscx_path)}")

    sig_n, sig_d = 4, 4
    qps = None  # negras por segundo; None = sin marca todavía
    infos = []
    for m in measures:
        ts = m.find('.//TimeSig')
        if ts is not None:
            n_el, d_el = ts.find('sigN'), ts.find('sigD')
            try:
                sig_n, sig_d = int(n_el.text), int(d_el.text)
            except (AttributeError, TypeError, ValueError):
                pass
        tp = m.find('.//Tempo/tempo')
        if tp is not None and tp.text:
            try:
                qps = float(tp.text)
            except ValueError:
                pass
        beats = sig_n * 4.0 / sig_d
        # Compás irregular (anacrusa / pickup): MuseScore guarda la duración
        # real en el atributo len="N/D" — ignorarlo desincroniza todo lo que
        # sigue, así que tiene prioridad sobre la cifra de compás.
        len_attr = m.get("len")
        if len_attr:
            try:
                num, den = len_attr.split("/")
                beats = int(num) * 4.0 / int(den)
            except (ValueError, ZeroDivisionError):
                pass
        # Ataques reales del compás (unión de las voces interpretables): con
        # ellos el playhead cae en la posición grabada de cada golpe, no en
        # una división uniforme del ancho. Se exige que al menos una voz
        # cubra el compás completo (la principal); las demás pueden ser
        # parciales.
        onsets, covered = set(), 0.0
        voices = m.findall('voice')
        if not voices and m.find('Chord') is not None:
            # MuseScore 2 no envuelve el contenido en <voice>: los acordes
            # cuelgan directo del compás — se trata el compás como una voz
            voices = [m]
        for v in voices:
            vo = _voice_onsets(v, beats)
            if vo is not None:
                onsets.update(vo[0])
                covered = max(covered, vo[1])
        ok = bool(onsets) and covered >= beats - 1e-4
        infos.append({"beats": beats, "qps": qps,
                      "onsets": sorted(onsets) if ok else None})

    # Expansión de repeticiones. Un endRepeat sin startRepeat previo repite
    # desde el comienzo (o desde el final de la repetición anterior), igual
    # que en la convención musical.
    played, i, last_start = [], 0, 0
    while i < len(measures):
        m = measures[i]
        if m.find('.//startRepeat') is not None:
            last_start = i
        end_el = m.find('.//endRepeat')
        if end_el is not None:
            txt = (end_el.text or "").strip()
            times = int(txt) if txt.isdigit() and int(txt) >= 2 else 2
            played.append(i)
            for _ in range(times - 1):
                played.extend(range(last_start, i + 1))
            last_start = i + 1
        else:
            played.append(i)
        i += 1
    return {"measures": infos, "n_measures": len(measures), "played": played}


def _extract_title(mscx_path):
    """Extrae el título de la canción del .mscx, limpiando vel= y saltos de línea."""
    try:
        with open(mscx_path, encoding="utf-8") as f:
            root = ET.fromstring(f.read())
        for tag in root.iter('metaTag'):
            if tag.get('name') == 'workTitle' and tag.text and tag.text.strip():
                return tag.text.strip()
        for elem in root.iter('Text'):
            sty = elem.find('style')
            txt = elem.find('text')
            if sty is not None and 'Title' in (sty.text or '') and txt is not None and txt.text:
                raw = txt.text
                raw = re.sub(r'\s*vel=\d+', '', raw)   # quitar vel=190 etc.
                raw = ' '.join(raw.split())            # colapsar espacios
                raw = raw.replace('"', '').strip()
                if raw:
                    return raw
    except Exception:
        pass
    return ""


def _load_fonts(count_size=140):
    """Fuentes del encabezado y del conteo, con candidatos para Linux,
    Windows y macOS. Devuelve (grande, chica, número_de_conteo)."""
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf"),
    ]
    for bold, regular in candidates:
        try:
            return (ImageFont.truetype(bold, 22),
                    ImageFont.truetype(regular, 17),
                    ImageFont.truetype(bold, count_size))
        except OSError:
            continue
    default = ImageFont.load_default()
    return default, default, default

def _match_onsets_to_columns(onsets, xs, beats, mx0, mx1, gap_px):
    """Alineamiento monótono tolerante entre ataques (tiempo) y columnas (x).

    1. Agrupa las x de cabezas/silencios en columnas (varias voces del mismo
       ataque están alineadas; el umbral escala con el tamaño de página).
    2. Normaliza ambos ejes a [0,1] y busca el emparejado monótono 1-a-1 de
       máxima cantidad y mínimo error cuadrático (DP tipo alineamiento de
       secuencias), permitiendo saltarse columnas o ataques sueltos.
    3. Acepta solo si el error medio es chico; devuelve [(ataque, x)] con los
       pares confiables — la interpolación cubre los ataques sin ancla.
    """
    # Con un solo ataque (silencio de compás entero) el reparto uniforme es
    # lo correcto: no hay golpes que clavar y el glifo del silencio se dibuja
    # centrado, no donde "suena".
    if not xs or not onsets or len(onsets) < 2:
        return None
    cols, cur = [], [xs[0]]
    for x in xs[1:]:
        if x - cur[-1] < gap_px * 1.1:
            cur.append(x)
        else:
            cols.append(cur)
            cur = [x]
    cols.append(cur)
    cx = [sum(c) / len(c) for c in cols]

    # Cuentas exactas → el emparejado 1:1 en orden es inambiguo (no dependen
    # de distancias: cubre el primer compás del sistema, donde la clave y la
    # cifra corren todas las columnas hacia la derecha).
    if len(cx) == len(onsets):
        return list(zip(onsets, cx))

    span_x = max(mx1 - mx0, 1e-6)
    po = [o / max(beats, 1e-6) for o in onsets]          # posición temporal 0..1
    pc = [(x - mx0) / span_x for x in cx]                # posición gráfica 0..1

    n, m = len(po), len(pc)
    GATE = 0.28   # distancia máxima aceptable de un par (fracción del compás)
    NEG = (-1, 0.0)
    D = [[NEG] * (m + 1) for _ in range(n + 1)]
    D[0][0] = (0, 0.0)
    for i in range(n + 1):
        for j in range(m + 1):
            cur_best = D[i][j]
            if cur_best[0] < 0:
                continue
            if j < m and D[i][j + 1] < cur_best:                 # columna sin usar
                D[i][j + 1] = cur_best
            if i < n and D[i + 1][j] < cur_best:                 # ataque sin ancla
                D[i + 1][j] = cur_best
            if i < n and j < m:
                d = abs(po[i] - pc[j])
                if d <= GATE:
                    cand = (cur_best[0] + 1, cur_best[1] - d * d)
                    if D[i + 1][j + 1] < cand:
                        D[i + 1][j + 1] = cand
    matched, score = D[n][m]
    if matched < 1 or matched < len(po) * 0.5:
        return None
    # reconstrucción del camino (greedy hacia atrás sobre la misma DP)
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        d = abs(po[i - 1] - pc[j - 1])
        cand = None
        if d <= GATE and D[i - 1][j - 1][0] >= 0:
            cand = (D[i - 1][j - 1][0] + 1, D[i - 1][j - 1][1] - d * d)
        if cand is not None and cand == D[i][j]:
            pairs.append((onsets[i - 1], cx[j - 1]))
            i, j = i - 1, j - 1
        elif D[i][j] == D[i][j - 1]:
            j -= 1
        else:
            i -= 1
    pairs.reverse()
    if not pairs:
        return None
    mean_err = sum(abs(o / max(beats, 1e-6) - (x - mx0) / span_x)
                   for o, x in pairs) / len(pairs)
    if mean_err > 0.15:
        return None
    return pairs


# ─── engine ───────────────────────────────────────────────────────────────────

class ScoreEngine:
    def __init__(self, cfg):
        self.cfg = cfg

    def build(self, phase=None):
        # `phase` (opcional): barra de progreso de este paso — ver progress.py.
        # La construcción reparte su avance: 20 % parseo SVG/XML, 80 % carga y
        # reescalado de las hojas PNG (lo más pesado).
        def _ph(frac, detail=""):
            if phase:
                phase.update(frac, detail)

        cfg = self.cfg
        file_nums = cfg["file_nums"]
        tpl = cfg["name_tpl"]

        _ph(0.02, "leyendo geometría (SVG)")
        self.layouts    = {i: _parse_svg_layout(f"{cfg['svg_dir']}/{tpl.format(i=i)}-1.svg")
                           for i in file_nums}
        _ph(0.10, "leyendo la música (MSCX)")
        self.score_data = {i: _parse_score_xml(f"{cfg['mscx_dir']}/{tpl.format(i=i)}.mscx")
                           for i in file_nums}
        _ph(0.20, "geometría por compás")
        self._fidx = {fn: idx for idx, fn in enumerate(file_nums)}

        # ── Geometría por compás ─────────────────────────────────────────────
        # Con las barras de compás del SVG, cada compás conoce su sistema y su
        # rango horizontal EXACTOS (MuseScore dibuja compases de anchos
        # distintos según la densidad de notas — suponer anchos uniformes
        # desvía el playhead). Solo se usa si la cuenta de barlines coincide
        # con la del XML; si no, se cae a la aproximación uniforme.
        self.measure_map = {}
        for fn in file_nums:
            lay = self.layouts[fn]
            n_m = self.score_data[fn]["n_measures"]
            mm = None
            if lay.get("sys_bounds"):
                total = sum(len(b) - 1 for b in lay["sys_bounds"])
                if total == n_m:
                    mm = []
                    for si, bounds in enumerate(lay["sys_bounds"]):
                        for j in range(len(bounds) - 1):
                            mm.append((si, bounds[j], bounds[j + 1]))
            if mm is None:
                n_s = len(lay["tops"])
                mps = max(1, n_m // n_s)
                per_sys = {}
                for m in range(n_m):
                    per_sys.setdefault(min(m // mps, n_s - 1), []).append(m)
                mm = [None] * n_m
                for si, ms_list in per_sys.items():
                    l = lay["lefts"][si]
                    r = lay["rights"][si]
                    w_m = (r - l) / len(ms_list)
                    for pos, m in enumerate(ms_list):
                        mm[m] = (si, l + pos * w_m, l + (pos + 1) * w_m)
            self.measure_map[fn] = mm

        # ── Mapa ataque→x por compás ─────────────────────────────────────────
        # Empareja los ataques del XML con las columnas de notas del SVG
        # mediante un alineamiento monótono tolerante: sobreviven columnas de
        # sobra (apoyaturas, cabezas desplazadas) y ataques sin columna. Si
        # un compás no alcanza la calidad mínima, queda None y se usa el
        # reparto uniforme (nunca peor que antes).
        self.beat_x_map = {}
        for fn in file_nums:
            lay = self.layouts[fn]
            sd  = self.score_data[fn]
            gap_px = lay.get("line_gap", 15.0)
            n_sys = len(lay["tops"])
            # Pertenencia vertical por sistema: cada sistema es dueño de la
            # franja hasta el punto medio con el vecino (los platillos y el
            # bombo se dibujan bien arriba/abajo del pentagrama).
            sys_y = []
            for si in range(n_sys):
                y_lo = (0.0 if si == 0
                        else (lay["bottoms"][si - 1] + lay["tops"][si]) / 2)
                y_hi = (lay["h"] if si == n_sys - 1
                        else (lay["bottoms"][si] + lay["tops"][si + 1]) / 2)
                sys_y.append((y_lo, y_hi))
            maps = []
            for m_idx, (si, mx0, mx1) in enumerate(self.measure_map[fn]):
                info = sd["measures"][m_idx]
                bmap = None
                if info.get("onsets") and lay.get("note_pts"):
                    y_lo, y_hi = sys_y[si]
                    xs = sorted(x for x, y in lay["note_pts"]
                                if mx0 + 1 <= x < mx1 - 1 and y_lo <= y <= y_hi)
                    bmap = _match_onsets_to_columns(
                        info["onsets"], xs, info["beats"], mx0, mx1, gap_px)
                maps.append(bmap)
            self.beat_x_map[fn] = maps

        # El tempo se hereda de página en página: cada archivo .mscz es una
        # hoja de la misma obra y normalmente solo la primera trae la marca.
        qps_cur = max(cfg["bpm"], 1) / 60.0
        for fn in file_nums:
            for m in self.score_data[fn]["measures"]:
                # tempo ≤ 0 = marca corrupta en el .mscx: se ignora y se
                # hereda el anterior (un qps negativo daría duraciones
                # negativas y rompería toda la línea de tiempo)
                if m["qps"] and m["qps"] > 0:
                    qps_cur = m["qps"]
                else:
                    m["qps"] = qps_cur

        self.song_name = cfg.get("song_name") or _extract_title(
            f"{cfg['mscx_dir']}/{tpl.format(i=file_nums[0])}.mscx") or "Scrolling Score"

        # Dimensiones del video
        fn0  = file_nums[0]
        lay0 = self.layouts[fn0]
        self._svg_scale = {i: cfg["video_w"] / self.layouts[i]["w"] for i in file_nums}
        sv0 = self._svg_scale[fn0]
        if len(lay0["tops"]) > 1:
            self.sys_spacing_px = (lay0["tops"][1] - lay0["tops"][0]) * sv0
        else:
            # Página de un solo sistema: estimar el interlineado a partir de
            # la altura del sistema para no dividir por cero ni fallar.
            self.sys_spacing_px = max(1.0, (lay0["bottoms"][0] - lay0["tops"][0]) * sv0 * 3.0)
        self.video_w = cfg["video_w"]
        # Resolución del dispositivo: si el usuario fijó video_h, el video se
        # genera exactamente a ese alto (la cantidad de líneas visibles surge
        # sola de la escala); si no, alto automático según n_visible_lines.
        if cfg.get("video_h"):
            self.video_h = int(cfg["video_h"])
        else:
            self.video_h = int(cfg["n_visible_lines"] * self.sys_spacing_px)
        self.video_h += self.video_h % 2
        frac = min(1.0, max(0.0, float(cfg["playhead_frac"])))
        self.playhead_y = int(self.video_h * frac)

        # ── Recorte de páginas según la brecha configurada ───────────────────
        # gap=0   → recortar exactamente en el borde del pentagrama (uniones).
        # gap=1   → conservar la hoja completa en las uniones.
        # El primer borde superior y el último inferior no son uniones: llevan
        # siempre un padding fijo razonable (_PAD_SVG).
        gap = min(1.0, max(0.0, float(cfg["page_gap_pct"]) / 100.0))
        n_pages = len(file_nums)
        self._crop_top_svg = {}
        self.page_px = {}      # dimensiones del PNG original de cada página
        cropped_imgs = []
        for idx, i in enumerate(file_nums):
            _ph(0.25 + 0.7 * idx / n_pages, f"cargando hoja {idx + 1}/{n_pages}")
            lay = self.layouts[i]
            top_edge, bot_edge = lay["tops"][0], lay["bottoms"][-1]
            if idx == 0:
                top_svg = max(0.0, top_edge - _PAD_SVG)
            else:
                top_svg = top_edge * (1.0 - gap)
            if idx == n_pages - 1:
                # Última página: conservar TODO lo que hay debajo del último
                # sistema (dibujos, símbolos, colas) — nunca recortar al ras.
                bot_svg = lay["h"]
            else:
                bot_svg = bot_edge + (lay["h"] - bot_edge) * gap

            raw = Image.open(f"{cfg['png_dir']}/{tpl.format(i=i)}-1.png").convert("RGBA")
            rgb = Image.fromarray(_compose_rgba(raw, cfg["score_bg"]))
            pw, ph = rgb.size
            self.page_px[i] = (pw, ph)
            top_png = max(0,  int(round(top_svg * ph / lay["h"])))
            bot_png = min(ph, int(round(bot_svg * ph / lay["h"])))
            if bot_png <= top_png:
                raise ValueError(f"Recorte vacío en la página {i} — SVG y PNG no coinciden")
            # Re-derivar el valor SVG desde el píxel real para que el mapeo
            # SVG→lienzo sea exacto (sin acumulación de errores de redondeo).
            self._crop_top_svg[i] = top_png * lay["h"] / ph
            cropped = rgb.crop((0, top_png, pw, bot_png))
            new_h = max(1, int(round(cropped.size[1] * cfg["video_w"] / pw)))
            cropped_imgs.append(cropped.resize((cfg["video_w"], new_h), Image.LANCZOS))

        # Las páginas recortadas se apilan una a continuación de la otra: el
        # espacio entre pentagramas de páginas contiguas es exactamente el
        # margen que la brecha decidió conservar.
        # El margen superior (playhead_y en blanco) permite que el PRIMER
        # sistema arranque ya posicionado en la línea lectora: sin él, la
        # vista queda clavada en el tope del lienzo al comienzo y la línea
        # actual "cae" hasta engancharse — se veía como una desincronización
        # que luego se corregía sola.
        pad_top = self.playhead_y
        self.page_y_offsets = [pad_top]
        for img in cropped_imgs[:-1]:
            self.page_y_offsets.append(self.page_y_offsets[-1] + img.size[1])

        # Lienzo (fondo blanco → uniones invisibles entre páginas)
        last_h  = cropped_imgs[-1].size[1]
        total_h = int(self.page_y_offsets[-1] + last_h + self.video_h)
        canvas  = Image.new("RGB", (self.video_w, total_h), cfg["bg"])
        for img, yo in zip(cropped_imgs, self.page_y_offsets):
            canvas.paste(img, (0, yo))
        self.canvas_np = np.array(canvas)[..., ::-1]
        self.canvas_h  = total_h

        # Keyframes de scroll + línea de tiempo musical
        _ph(0.96, "calculando el scroll")
        self._build_keyframes()

        # ── Conteo previo: desplaza toda la música `lead_in` segundos ────────
        # Los pulsos van al tempo real del primer compás, así el conteo ES la
        # referencia exacta con la que arranca la partitura.
        self.count_beats = max(0, min(16, int(cfg.get("count_in_beats", 0))))
        self.lead_in = 0.0
        if self.count_beats > 0:
            qps0 = self.score_data[fn0]["measures"][0]["qps"]
            # Un pulso extra de anticipación ANTES de la cuenta: el video no
            # arranca contando de golpe, da un instante para reaccionar →
            #   (prep)··4··3··2··1··arranca   en vez de   4·3·2·1·arranca
            self.count_slots = self.count_beats + 1
            lead = self.count_slots / qps0
            self.lead_in = lead
            self.keyframes = ([(0.0, self.keyframes[0][1])] +
                              [(t + lead, y) for t, y in self.keyframes])
            self._kf_times = [k[0] for k in self.keyframes]
            self._timeline = [(t + lead, dur, fn, mi, b, bpm)
                              for (t, dur, fn, mi, b, bpm) in self._timeline]
            self._tl_times = [e[0] for e in self._timeline]
            self._music_end_t += lead

        self._build_slopes()
        self.total_duration = self._music_end_t  # termina con la última nota

        # Momento en que se activa la segunda línea → dispara el fundido del
        # encabezado (así el título no tapa el primer sistema al arrancar).
        self._t_second_line = self._music_end_t
        for t, _dur, fn, mi, _beats, _bpm in self._timeline:
            if fn != fn0 or self.measure_map[fn][mi][0] >= 1:
                self._t_second_line = t
                break

        count_size = max(64, int(self.video_h * 0.30))
        self._font_lg, self._font_sm, self._font_count = _load_fonts(count_size)
        self._hdr_cache = {}
        return self

    # ── coords ────────────────────────────────────────────────────────────────
    def _canvas_y(self, fn, fidx, svg_y):
        return self.page_y_offsets[fidx] + (svg_y - self._crop_top_svg[fn]) * self._svg_scale[fn]

    def _sys_top(self, fn, fidx, si):
        return self._canvas_y(fn, fidx, self.layouts[fn]["tops"][si])

    def _sys_bot(self, fn, fidx, si):
        return self._canvas_y(fn, fidx, self.layouts[fn]["bottoms"][si])

    # ── keyframes ─────────────────────────────────────────────────────────────
    def _build_keyframes(self):
        """Construye dos estructuras a partir de la partitura:

        self.keyframes: [(t, y)] — posición vertical objetivo del playhead en
            el lienzo al comienzo de cada compás reproducido (más keyframes
            auxiliares para los saltos de repetición).
        self._timeline: [(t, dur, fn, mi, beats, bpm)] — un registro por compás
            reproducido, para playhead, puntos de pulso y encabezado.
        """
        cfg = self.cfg
        file_nums = cfg["file_nums"]

        # Posición de cada compás dentro de su sistema (índice y total), a
        # partir del mapa exacto de geometría.
        sys_count, sys_pos = {}, {}
        for fn in file_nums:
            counts = {}
            for m_idx, (si, _x0, _x1) in enumerate(self.measure_map[fn]):
                sys_pos[(fn, m_idx)] = counts.get(si, 0)
                counts[si] = counts.get(si, 0) + 1
            for si, c in counts.items():
                sys_count[(fn, si)] = c

        # Sistemas que contienen repeticiones (se recorren más de una pasada):
        # en ellos el avance se reparte por número de pasada, no por compás.
        total_plays, repeated_sys = {}, set()
        for fn in file_nums:
            sd = self.score_data[fn]
            for m in sd["played"]:
                s = self.measure_map[fn][m][0]
                total_plays[(fn, s)] = total_plays.get((fn, s), 0) + 1
        for (f, s), c in total_plays.items():
            if c > sys_count[(f, s)]:
                repeated_sys.add((f, s))

        # Índice global de sistemas para conocer el "sistema siguiente"
        all_sys, sys_gidx = [], {}
        for fidx, fn in enumerate(file_nums):
            for si in range(len(self.layouts[fn]["tops"])):
                sys_gidx[(fn, si)] = len(all_sys)
                all_sys.append(self._sys_top(fn, fidx, si))

        base_kf, timeline = [], []
        t, sys_play_count = 0.0, {}
        for fidx, fn in enumerate(file_nums):
            sd = self.score_data[fn]
            for m_idx in sd["played"]:
                info  = sd["measures"][m_idx]
                dur   = info["beats"] / info["qps"]      # segundos de este compás
                sys_i = self.measure_map[fn][m_idx][0]
                key   = (fn, sys_i)
                count_before = sys_play_count.get(key, 0)
                total = total_plays.get(key, 1)
                n_in_sys = sys_count[key]
                frac  = (sys_pos[(fn, m_idx)] / n_in_sys if key not in repeated_sys
                         else count_before / total)
                y0 = self._sys_top(fn, fidx, sys_i)
                gi = sys_gidx[key]
                # El ÚLTIMO sistema global no tiene "sistema siguiente" al que
                # avanzar: se fija en la línea lectora (y1 = y0) para que el
                # scroll se detenga ahí, con la última línea centrada y el resto
                # de la hoja debajo, en vez de seguir bajando al vacío.
                y1 = all_sys[gi + 1] if gi + 1 < len(all_sys) else y0
                base_kf.append((t, y0 + (y1 - y0) * frac))
                timeline.append((t, dur, fn, m_idx, info["beats"], round(info["qps"] * 60)))
                sys_play_count[key] = count_before + 1
                t += dur
        self._music_end_t = t

        # Saltos de repetición hacia atrás: mantener la lectura de la línea
        # actual hasta el 70 % del compás y volver atrás solo en el 30 % final,
        # en vez de untar el retroceso a lo largo de todo el compás.
        kf = []
        for i in range(len(base_kf) - 1):
            t0, y0 = base_kf[i]
            t1, y1 = base_kf[i + 1]
            kf.append((t0, y0))
            if y1 < y0 - 0.5 * self.sys_spacing_px:
                rate = 0.0
                if i > 0:
                    tp, yp = base_kf[i - 1]
                    if y0 > yp:
                        rate = (y0 - yp) / max(t0 - tp, 1e-9)
                kf.append((t0 + 0.7 * (t1 - t0), y0 + rate * 0.7 * (t1 - t0)))
        kf.append(base_kf[-1])
        # Keyframe extra para una cola de interpolación suave (solo se usa
        # para interpolar — total_duration termina ANTES de este punto).
        kf.append((self._music_end_t + 1.0, kf[-1][1]))

        self.keyframes = kf
        self._kf_times = [k[0] for k in kf]
        self._timeline = timeline
        self._tl_times = [e[0] for e in timeline]

    # ── pendientes monótonas (spline de Hermite) ──────────────────────────────
    def _build_slopes(self):
        kf = self.keyframes
        n  = len(kf)
        ys = [k[1] for k in kf]
        ts = [k[0] for k in kf]
        delta = [(ys[i + 1] - ys[i]) / max(ts[i + 1] - ts[i], 1e-9) for i in range(n - 1)]
        m = [delta[0]] + [(delta[i - 1] + delta[i]) / 2 for i in range(1, n - 1)] + [delta[-1]]
        for i in range(n - 1):
            if abs(delta[i]) < 1e-9:
                m[i] = m[i + 1] = 0.0
            else:
                a, b = m[i] / delta[i], m[i + 1] / delta[i]
                s = a * a + b * b
                if s > 9:
                    t3 = 3.0 / math.sqrt(s)
                    m[i]     = t3 * a * delta[i]
                    m[i + 1] = t3 * b * delta[i]
        self._slopes = m

    def _scroll_y_at(self, ts):
        kf = self.keyframes
        if ts <= kf[0][0]:
            return kf[0][1]
        if ts >= kf[-1][0]:
            return kf[-1][1]
        lo = bisect.bisect_right(self._kf_times, ts) - 1
        lo = max(0, min(lo, len(kf) - 2))
        t0, y0 = kf[lo]
        t1, y1 = kf[lo + 1]
        dt = t1 - t0
        s  = (ts - t0) / dt if dt > 0 else 1.0
        m0 = self._slopes[lo] * dt
        m1 = self._slopes[lo + 1] * dt
        return ((2 * s**3 - 3 * s**2 + 1) * y0 + (s**3 - 2 * s**2 + s) * m0 +
                (-2 * s**3 + 3 * s**2) * y1 + (s**3 - s**2) * m1)

    def _timeline_at(self, ts):
        i = bisect.bisect_right(self._tl_times, ts) - 1
        return self._timeline[max(0, min(i, len(self._timeline) - 1))]

    def _measure_x_at(self, fn, mi, m_prog, beats, mx0, mx1):
        """x del playhead dentro del compás (unidades SVG): interpola entre
        las posiciones grabadas de los ataques; sin mapa, reparto uniforme."""
        bmap = self.beat_x_map[fn][mi]
        if not bmap:
            return mx0 + m_prog * (mx1 - mx0)
        bpos = m_prog * beats
        if bpos <= bmap[0][0]:
            return bmap[0][1]
        pts = bmap + [(beats, mx1)]
        for k in range(len(pts) - 1):
            b0, x0 = pts[k]
            b1, x1 = pts[k + 1]
            if bpos <= b1:
                f = (bpos - b0) / max(b1 - b0, 1e-9)
                return x0 + f * (x1 - x0)
        return mx1

    # ── encabezado ────────────────────────────────────────────────────────────
    def _header_strip(self, fn, bpm):
        """Barra de encabezado ya renderizada (BGR). Se cachea por página/tempo."""
        key = (fn, bpm)
        cached = self._hdr_cache.get(key)
        if cached is not None:
            return cached
        W = self.video_w
        hdr = np.zeros((_HEADER_H, W, 3), dtype=np.uint8)
        for row in range(_HEADER_H):  # degradé: carbón oscuro → casi negro
            v = int(12 + 8 * (row / _HEADER_H))
            hdr[row] = [v, v, v]
        hdr[-2:] = [0, 130, 255]      # línea de acento naranja (BGR)

        h_pil  = Image.fromarray(hdr[..., ::-1])
        h_draw = ImageDraw.Draw(h_pil)
        h_draw.text((16, (_HEADER_H - 17) // 2), f"Página {fn}",
                    font=self._font_sm, fill=(160, 190, 220))
        bpm_text = f"{bpm} BPM"
        try:
            bw = h_draw.textlength(bpm_text, font=self._font_sm)
        except AttributeError:
            bw = len(bpm_text) * 10
        h_draw.text((W - bw - 16, (_HEADER_H - 17) // 2), bpm_text,
                    font=self._font_sm, fill=(255, 190, 50))
        side_w   = int(W * 0.22)
        center_w = W - 2 * side_w
        cname = self.song_name
        try:
            while len(cname) > 4:
                if h_draw.textlength(cname, font=self._font_lg) <= center_w - 20:
                    break
                cname = cname[:-1]
            if len(cname) < len(self.song_name):
                cname = cname.rstrip() + "…"
        except AttributeError:
            pass
        h_draw.text((side_w + center_w // 2, _HEADER_H // 2), cname,
                    font=self._font_lg, fill=(255, 245, 210), anchor="mm")
        strip = np.array(h_pil)[..., ::-1]
        self._hdr_cache[key] = strip
        return strip

    # ── frame renderer ────────────────────────────────────────────────────────
    def render_frame(self, time_s):
        cfg = self.cfg

        # ── Recorte del lienzo con scroll SUB-PÍXEL ──────────────────────────
        # Truncar el desplazamiento a un entero hacía que a baja velocidad
        # (~1 px/frame) muchos frames no avanzaran nada y otros saltaran 2 px →
        # se veía "a saltitos". Interpolando linealmente entre las dos filas
        # vecinas el movimiento queda perfectamente continuo.
        scroll_f = self._scroll_y_at(time_s) - self.playhead_y
        scroll_f = min(max(scroll_f, 0.0), float(self.canvas_h - self.video_h))
        fs = int(scroll_f)
        # Peso de interpolación en 1/256avos: la mezcla se hace con enteros
        # uint16 (a*(256-w) + b*w) >> 8 — visualmente idéntica a la float pero
        # ~3x más rápida (el render por frame es el costo dominante del video).
        w = int((scroll_f - fs) * 256.0 + 0.5)
        if w >= 256:
            fs += 1
            w = 0
        if w <= 0 or fs + 1 + self.video_h > self.canvas_h:
            # .copy(): el frame se pinta encima (header, puntos, playhead) y
            # NUNCA debe compartir memoria con el lienzo maestro
            frame = self.canvas_np[fs:fs + self.video_h].copy()
        else:
            a = self.canvas_np[fs:fs + self.video_h].astype(np.uint16)
            a *= 256 - w
            a += self.canvas_np[fs + 1:fs + 1 + self.video_h].astype(np.uint16) * w
            a >>= 8
            frame = a.astype(np.uint8)

        # Compás activo
        t0, dur, fn, mi, beats, bpm = self._timeline_at(time_s)
        m_prog = min(1.0, max(0.0, (time_s - t0) / dur)) if dur > 0 else 0.0
        fidx  = self._fidx[fn]
        sys_i, mx0, mx1 = self.measure_map[fn][mi]
        ct = self._sys_top(fn, fidx, sys_i) - scroll_f
        cb = self._sys_bot(fn, fidx, sys_i) - scroll_f
        pad = 18
        ht = max(0, int(ct) - pad)
        hb = min(self.video_h, int(cb) + pad)

        # ── Conteo previo ────────────────────────────────────────────────────
        # Velo translúcido (la partitura sigue visible para pre-leer), número
        # gigante que pulsa en cada tiempo y fila de puntos de progreso. En el
        # último 40 % del último pulso todo se desvanece: cuando suena el
        # primer compás la partitura está completamente limpia.
        in_count = self.lead_in > 0 and time_s < self.lead_in
        if in_count:
            slots = self.count_slots                 # count_beats + 1 (prep)
            spb   = self.lead_in / slots
            slot  = min(slots - 1, int(time_s / spb))
            bp    = (time_s - slot * spb) / spb
            is_prep = (slot == 0)                     # primer pulso = anticipación
            # número que se muestra: prep→(nada); slots 1..N → N..1
            number = None if is_prep else (self.count_beats - (slot - 1))
            fade = 1.0
            if slot == slots - 1 and bp > 0.6:        # desvanecer en el último
                fade = max(0.0, 1.0 - (bp - 0.6) / 0.4)
            veil = (0.30 if is_prep else 0.45) * fade
            if veil > 0.004:
                frame[:] = (frame.astype(np.float32) * (1 - veil)
                            + 255.0 * veil).astype(np.uint8)
            f_pil = Image.fromarray(frame[..., ::-1])
            d = ImageDraw.Draw(f_pil, "RGBA")
            cx, cy = self.video_w // 2, int(self.video_h * 0.42)
            if number is not None:
                num_alpha = fade * (0.60 + 0.40 * max(0.0, 1.0 - bp * 3.0))
                txt = str(number)
                d.text((cx + 3, cy + 4), txt, font=self._font_count, anchor="mm",
                       fill=(40, 40, 40, int(90 * num_alpha)))
                d.text((cx, cy), txt, font=self._font_count, anchor="mm",
                       fill=(255, 140, 0, int(255 * num_alpha)))
            else:
                # anticipación: "¿Listo?" tenue, sin número que contar todavía
                try:
                    d.text((cx, cy), "¿Listo?", font=self._font_lg, anchor="mm",
                           fill=(255, 200, 120, int(180 * (0.5 + 0.5 * bp))))
                except Exception:
                    pass
            # puntos de progreso: uno por pulso contado; el pulso actual (slot-1)
            # se resalta. En la anticipación todos van tenues.
            dr2, gap2 = 9, 36
            y_dots = cy + int(self.video_h * 0.42 * 0.55)
            x0d = cx - (self.count_beats * gap2) // 2
            active = slot - 1                          # -1 durante la anticipación
            for i in range(self.count_beats):
                cxd = x0d + i * gap2 + gap2 // 2
                if i < active:
                    col = (255, 140, 0, int(200 * fade)); r_i = dr2
                elif i == active:
                    col = (255, 140, 0, int(255 * fade)); r_i = dr2 + int(3 * max(0.0, 1.0 - bp * 2.0))
                else:
                    col = (150, 150, 150, int(140 * fade)); r_i = dr2 - 2
                d.ellipse([(cxd - r_i, y_dots - r_i), (cxd + r_i, y_dots + r_i)],
                          fill=col)
            frame = np.array(f_pil)[..., ::-1].copy()
            return frame

        # ── Encabezado: aparece cuando empieza la segunda línea ──────────────
        h_prog = max(0.0, min(1.0, (time_s - self._t_second_line) / _FADE_DUR))
        # (con un video más bajo que el encabezado no hay dónde dibujarlo)
        if h_prog > 0.01 and cfg.get("show_header", True) and self.video_h > _HEADER_H + 20:
            hdr = self._header_strip(fn, bpm)
            if h_prog >= 0.995:
                frame[:_HEADER_H] = hdr        # fundido terminado: copia directa
            else:
                region = frame[:_HEADER_H].astype(np.float32)
                frame[:_HEADER_H] = (region * (1 - h_prog) +
                                     hdr.astype(np.float32) * h_prog).astype(np.uint8)

        # ── Puntos de pulso (blit directo con máscara circular, sin PIL) ─────
        nb   = _beat_count(beats)
        beat = min(int(m_prog * nb), nb - 1)
        dr, gap_px = 8, 22
        mask = getattr(self, "_dot_mask", None)
        if mask is None:
            yy, xx = np.mgrid[-dr:dr + 1, -dr:dr + 1]
            mask = self._dot_mask = (xx * xx + yy * yy) <= dr * dr + dr // 2
        x0  = (self.video_w - nb * gap_px) // 2
        cy2 = self.video_h - 30
        for b in range(nb):
            cx3 = x0 + b * gap_px + gap_px // 2
            clr = (50, 190, 255) if b == beat else (55, 55, 55)   # frame es BGR
            y0d, x0d = cy2 - dr, cx3 - dr
            if y0d < 0 or x0d < 0 or cx3 + dr + 1 > self.video_w:
                continue
            frame[y0d:cy2 + dr + 1, x0d:cx3 + dr + 1][mask] = clr

        # ── Línea vertical de playhead ───────────────────────────────────────
        # Rango exacto del compás (barlines) + posiciones grabadas de cada
        # ataque (mapa nota→x): la línea cae donde está el golpe de verdad.
        pw = cfg["playhead_w"]
        if pw > 0:
            sv = self._svg_scale[fn]
            if cfg.get("playhead_mode") == "beats":
                # de a saltitos: la línea queda clavada en el tiempo actual
                nbq = _beat_count(beats)
                bq  = min(nbq - 1, int(m_prog * nbq + 1e-6))
                x_svg = self._measure_x_at(fn, mi, bq / nbq, beats, mx0, mx1)
            else:
                x_svg = self._measure_x_at(fn, mi, m_prog, beats, mx0, mx1)
            vl_x = max(0, min(self.video_w - 1, int(x_svg * sv)))
            eff_hdr = int(_HEADER_H * h_prog) + 4
            vl_top  = max(eff_hdr, ht)
            vl_bot  = min(self.video_h - 1, hb)
            pc = cfg["playhead_color"]
            alpha = min(1.0, max(0.05, float(cfg.get("playhead_alpha", 1.0))))
            x0c, x1c = max(0, vl_x - pw), min(self.video_w, vl_x + pw)
            if vl_top < vl_bot and x0c < x1c:
                if alpha >= 0.995:
                    frame[vl_top:vl_bot, x0c:x1c] = [pc[2], pc[1], pc[0]]
                else:
                    region = frame[vl_top:vl_bot, x0c:x1c].astype(np.float32)
                    col = np.array([pc[2], pc[1], pc[0]], dtype=np.float32)
                    frame[vl_top:vl_bot, x0c:x1c] = (
                        region * (1 - alpha) + col * alpha).astype(np.uint8)

        return frame


def build_engine(overrides=None, phase=None):
    cfg = {**DEFAULT_CONFIG, **(overrides or {})}
    return ScoreEngine(cfg).build(phase=phase)

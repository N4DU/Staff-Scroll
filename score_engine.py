"""score_engine.py  v5"""
import os, re, math
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import xml.etree.ElementTree as ET

DEFAULT_CONFIG = {
    "bpm":               120,
    "fps":               30,
    "video_w":           1080,
    "n_visible_lines":   4,
    # ↓↓  READING LINE POSITION: 0.0=top  0.5=center  1.0=bottom  ↓↓
    "playhead_frac":     0.5,
    "page_gap_extra_px": 40,      # ← configurable extra gap between pages
    "score_bg":          (255, 255, 255),
    "bg":                (255, 255, 255),  # canvas bg = white → seamless gaps
    "playhead_color":    (255, 155, 0),
    "playhead_w":        3,
    "highlight_alpha":   0.0,
    "song_name":         "",       # auto-extracted if empty
    "show_header":       True,     # set False to hide the info bar entirely
    # Paths — set dynamically per job, do not rely on defaults
    "mscx_dir":   None,   # dir containing patched .mscx files
    "png_dir":    None,   # dir containing rendered .png files
    "svg_dir":    None,   # dir containing rendered .svg files
    "file_nums":  None,   # list of ints matching filenames, e.g. [1,2,3]
    "name_tpl":   "{i}-ThatBand",  # filename template, {i} = file number
}

_PAD_SVG = 120   # SVG units of crop padding around staff content (captures all notes)

# ─── helpers ─────────────────────────────────────────────────────────────────

def _compose_rgba(img_rgba, bg=(255,255,255)):
    arr = np.array(img_rgba, dtype=np.float32)
    rgb, a = arr[:,:,:3], arr[:,:,3:4]/255.0
    return (rgb*a + np.array(bg,dtype=np.float32)*(1-a)).astype(np.uint8)

def _parse_svg_layout(svg_path):
    with open(svg_path) as f: content = f.read()
    # MuseScore 3 exports "WIDTHpx"; MuseScore 4 exports "WIDTHmm" — use viewBox for coordinates
    h_m = re.search(r'height="([\d.]+)px"', content)
    w_m = re.search(r'width="([\d.]+)px"',  content)
    if h_m and w_m:
        h, w = float(h_m.group(1)), float(w_m.group(1))
    else:
        vb = re.search(r'viewBox="[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)"', content)
        w, h = float(vb.group(1)), float(vb.group(2))
    pts_list = re.findall(r'polyline class="StaffLines"[^>]*points="([^"]+)"', content)
    x_vals, y_set = [], set()
    for pts in pts_list:
        for coord in pts.strip().split():
            x,y = coord.split(','); x_vals.append(float(x)); y_set.add(float(y))
    y_sorted = sorted(y_set)
    # Adaptive threshold: works for both MuseScore 3 (px) and MuseScore 4 (viewBox units).
    # Within a system, lines are closely spaced; between systems the gap is much larger.
    # The median inter-line difference is always a within-system gap, so 1.5× is a safe cutoff.
    diffs = [y_sorted[i+1] - y_sorted[i] for i in range(len(y_sorted)-1)]
    median_diff = sorted(diffs)[len(diffs)//2] if diffs else 60
    threshold = max(60, median_diff * 1.5)
    systems, cur = [], [y_sorted[0]]
    for y in y_sorted[1:]:
        if y - cur[-1] < threshold: cur.append(y)
        else: systems.append(cur); cur = [y]
    systems.append(cur)
    return {"w":w, "h":h,
            "tops":    [min(s) for s in systems],
            "bottoms": [max(s) for s in systems],
            "left_x":  min(x_vals) if x_vals else 0,
            "right_x": max(x_vals) if x_vals else w}

def _parse_score_xml(mscx_path):
    with open(mscx_path) as f: root = ET.fromstring(f.read())
    beats_pm = (int(root.find('.//TimeSig/sigN').text) *
                4.0 / int(root.find('.//TimeSig/sigD').text))
    measures = root.findall('.//Measure')
    played, i = [], 0
    while i < len(measures):
        m = measures[i]; has_start = m.find('.//startRepeat') is not None
        end_el = m.find('.//endRepeat')
        if has_start:
            start_i = i; found = False
            for j in range(i, len(measures)):
                if measures[j].find('.//endRepeat') is not None:
                    times = int(measures[j].find('.//endRepeat').text or 2)
                    for _ in range(times): played.extend(range(start_i, j+1))
                    i = j+1; found = True; break
            if not found: played.append(i); i += 1
        else: played.append(i); i += 1
    return {"beats_pm": beats_pm, "n_measures": len(measures), "played": played}

def _extract_title(mscx_path):
    """Extract song title from mscx, clean up vel= and newlines."""
    try:
        with open(mscx_path) as f: root = ET.fromstring(f.read())
        for tag in root.iter('metaTag'):
            if tag.get('name') == 'workTitle' and tag.text and tag.text.strip():
                return tag.text.strip()
        for elem in root.iter('Text'):
            sty = elem.find('style'); txt = elem.find('text')
            if sty is not None and 'Title' in (sty.text or '') and txt is not None and txt.text:
                raw = txt.text
                raw = re.sub(r'\s*vel=\d+', '', raw)  # remove vel=190 etc
                raw = ' '.join(raw.split())            # collapse whitespace
                raw = raw.replace('"', '').strip()
                if raw: return raw
    except: pass
    return ""

# ─── engine ───────────────────────────────────────────────────────────────────

class ScoreEngine:
    def __init__(self, cfg): self.cfg = cfg

    def build(self):
        cfg = self.cfg; file_nums = cfg["file_nums"]

        tpl = cfg["name_tpl"]
        self.layouts    = {i: _parse_svg_layout(f"{cfg['svg_dir']}/{tpl.format(i=i)}-1.svg")
                           for i in file_nums}
        self.score_data = {i: _parse_score_xml(f"{cfg['mscx_dir']}/{tpl.format(i=i)}.mscx")
                           for i in file_nums}

        # Auto-detect song name
        tpl = cfg["name_tpl"]
        self.song_name = cfg.get("song_name") or _extract_title(
            f"{cfg['mscx_dir']}/{tpl.format(i=file_nums[0])}.mscx") or "Scrolling Score"

        # Video dims
        fn0  = file_nums[0]; lay0 = self.layouts[fn0]
        self._svg_scale    = {i: cfg["video_w"]/self.layouts[i]["w"] for i in file_nums}
        self._crop_top_svg = {i: self.layouts[i]["tops"][0] - _PAD_SVG for i in file_nums}
        self.sys_spacing_px = (lay0["tops"][1]-lay0["tops"][0]) * self._svg_scale[fn0]
        self.video_w   = cfg["video_w"]
        self.video_h   = int(cfg["n_visible_lines"] * self.sys_spacing_px)
        self.video_h  += self.video_h % 2
        self.playhead_y = int(self.video_h * cfg["playhead_frac"])

        # Load + crop pages
        cropped_imgs = []
        for i in file_nums:
            raw = Image.open(f"{cfg['png_dir']}/{tpl.format(i=i)}-1.png").convert("RGBA")
            rgb = Image.fromarray(_compose_rgba(raw, cfg["score_bg"]))
            pw, ph = rgb.size; lay = self.layouts[i]
            top_png = max(0,   int((lay["tops"][0]    -_PAD_SVG)*ph/lay["h"]))
            bot_png = min(ph,  int((lay["bottoms"][-1]+_PAD_SVG)*ph/lay["h"]))
            cropped = rgb.crop((0, top_png, pw, bot_png))
            new_h   = int(cropped.size[1] * cfg["video_w"] / pw)
            cropped_imgs.append(cropped.resize((cfg["video_w"], new_h), Image.LANCZOS))

        # Page offsets: inter-page system spacing = intra-page + page_gap_extra_px
        extra = cfg["page_gap_extra_px"]
        self.page_y_offsets = [0]
        for N in range(len(file_nums)-1):
            fn_N  = file_nums[N]; fn_N1 = file_nums[N+1]
            sv_N  = self._svg_scale[fn_N]; sv_N1 = self._svg_scale[fn_N1]
            ct_N  = self._crop_top_svg[fn_N]; ct_N1 = self._crop_top_svg[fn_N1]
            last_top = self.page_y_offsets[N] + (self.layouts[fn_N]["tops"][-1]-ct_N)*sv_N
            next_off = last_top + self.sys_spacing_px + extra - _PAD_SVG*sv_N1
            self.page_y_offsets.append(int(next_off))

        # Canvas (white bg → seamless between pages)
        last_h   = cropped_imgs[-1].size[1]
        total_h  = int(self.page_y_offsets[-1] + last_h + self.video_h)
        canvas   = Image.new("RGB", (self.video_w, total_h), cfg["bg"])
        for img, yo in zip(cropped_imgs, self.page_y_offsets):
            canvas.paste(img, (0, yo))
        self.canvas_np = np.array(canvas)[..., ::-1]
        self.canvas_h  = total_h

        # Cache first system y for header animation
        self._first_sys_y = self._sys_top(file_nums[0], 0, 0)

        # Time when second staff line first becomes active → triggers header fade
        fn0  = file_nums[0]; sd0 = self.score_data[fn0]
        lay0 = self.layouts[fn0]; n_s0 = len(lay0["tops"])
        mps0 = max(1, sd0["n_measures"] // n_s0)
        _t2  = 0.0; _spb2 = 60.0 / cfg["bpm"]
        self._t_second_line = sd0["beats_pm"] * _spb2 * len(sd0["played"])  # fallback
        for _m in sd0["played"]:
            if min(_m // mps0, n_s0-1) >= 1:
                self._t_second_line = _t2; break
            _t2 += sd0["beats_pm"] * _spb2

        # Keyframes + monotone slopes
        self.keyframes      = self._build_keyframes()
        self.total_duration = self._music_end_t  # ends at last note (no looping tail)
        self._build_slopes()

        # Fonts
        try:
            self._font_lg = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            self._font_sm = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
        except:
            self._font_lg = self._font_sm = ImageFont.load_default()
        return self

    # ── coords ────────────────────────────────────────────────────────────────
    def _canvas_y(self, fn, fidx, svg_y):
        return self.page_y_offsets[fidx] + (svg_y - self._crop_top_svg[fn])*self._svg_scale[fn]
    def _sys_top(self, fn, fidx, si):
        return self._canvas_y(fn, fidx, self.layouts[fn]["tops"][si])
    def _sys_bot(self, fn, fidx, si):
        return self._canvas_y(fn, fidx, self.layouts[fn]["bottoms"][si])

    # ── keyframes ─────────────────────────────────────────────────────────────
    def _build_keyframes(self):
        cfg = self.cfg; file_nums = cfg["file_nums"]; spb = 60.0/cfg["bpm"]
        repeated_sys = set()
        for fn in file_nums:
            sd = self.score_data[fn]; lay = self.layouts[fn]
            n_s = len(lay["tops"]); mps = max(1, sd["n_measures"]//n_s)
            counts = {}
            for m in sd["played"]:
                s = min(m//mps, n_s-1); counts[s] = counts.get(s,0)+1
            for s,c in counts.items():
                if c > mps: repeated_sys.add((fn,s))
        total_plays = {}
        for fn in file_nums:
            sd = self.score_data[fn]; lay = self.layouts[fn]
            n_s = len(lay["tops"]); mps = max(1, sd["n_measures"]//n_s)
            for m in sd["played"]:
                s = min(m//mps, n_s-1)
                total_plays[(fn,s)] = total_plays.get((fn,s),0)+1
        all_sys = []
        for fidx,fn in enumerate(file_nums):
            for si in range(len(self.layouts[fn]["tops"])):
                all_sys.append((fn, fidx, si, self._sys_top(fn, fidx, si)))
        def gidx(fn, si):
            for gi,(f,_,s,_) in enumerate(all_sys):
                if f==fn and s==si: return gi
            return len(all_sys)-1
        keyframes = []; t = 0.0; sys_play_count = {}
        for fidx, fn in enumerate(file_nums):
            sd = self.score_data[fn]; lay = self.layouts[fn]
            n_s = len(lay["tops"]); mps = max(1, sd["n_measures"]//n_s)
            spm = sd["beats_pm"]*spb
            for m_idx in sd["played"]:
                sys_i = min(m_idx//mps, n_s-1); key = (fn, sys_i)
                count_before = sys_play_count.get(key, 0)
                total = total_plays.get(key, mps)
                frac = (m_idx%mps)/mps if key not in repeated_sys else count_before/total
                y0 = self._sys_top(fn, fidx, sys_i)
                gi = gidx(fn, sys_i)
                y1 = all_sys[gi+1][3] if gi+1 < len(all_sys) else y0+self.sys_spacing_px
                keyframes.append((t, y0+(y1-y0)*frac, fn, m_idx))
                sys_play_count[key] = count_before+1; t += spm
        # Real music ends here. Append one more keyframe for smooth Hermite tail
        # (used only for interpolation — total_duration stops BEFORE this point).
        self._music_end_t = t
        keyframes.append((t + 1.0, keyframes[-1][1], keyframes[-1][2], keyframes[-1][3]))
        return keyframes

    # ── monotone slopes ───────────────────────────────────────────────────────
    def _build_slopes(self):
        kf = self.keyframes; n = len(kf)
        ys = [k[1] for k in kf]; ts = [k[0] for k in kf]
        delta = [(ys[i+1]-ys[i])/max(ts[i+1]-ts[i],1e-9) for i in range(n-1)]
        m = [delta[0]]+[(delta[i-1]+delta[i])/2 for i in range(1,n-1)]+[delta[-1]]
        for i in range(n-1):
            if abs(delta[i]) < 1e-9: m[i] = m[i+1] = 0.0
            else:
                a,b = m[i]/delta[i], m[i+1]/delta[i]; s = a*a+b*b
                if s > 9:
                    t3 = 3.0/math.sqrt(s); m[i]=t3*a*delta[i]; m[i+1]=t3*b*delta[i]
        self._slopes = m

    def _scroll_y_at(self, ts):
        kf = self.keyframes
        if ts <= kf[0][0]:  return kf[0][1]
        if ts >= kf[-1][0]: return kf[-1][1]
        lo,hi = 0, len(kf)-2
        while lo < hi:
            mid=(lo+hi)//2
            if kf[mid+1][0] <= ts: lo=mid+1
            else: hi=mid
        t0,y0=kf[lo][0],kf[lo][1]; t1,y1=kf[lo+1][0],kf[lo+1][1]
        dt=t1-t0; s=(ts-t0)/dt if dt>0 else 1.0
        m0=self._slopes[lo]*dt; m1=self._slopes[lo+1]*dt
        return (2*s**3-3*s**2+1)*y0+(s**3-2*s**2+s)*m0+(-2*s**3+3*s**2)*y1+(s**3-s**2)*m1

    def _active_at(self, ts):
        kf = self.keyframes
        for i in range(len(kf)-1):
            if kf[i][0] <= ts < kf[i+1][0]: return kf[i][2], kf[i][3]
        return kf[-1][2], kf[-1][3]

    # ── frame renderer ────────────────────────────────────────────────────────
    def render_frame(self, time_s):
        cfg = self.cfg
        scroll_y = self._scroll_y_at(time_s)
        fs = max(0, min(int(scroll_y)-self.playhead_y, self.canvas_h-self.video_h))

        # Crop canvas
        frame = np.empty((self.video_h, self.video_w, 3), dtype=np.uint8)
        bg = [cfg["bg"][2], cfg["bg"][1], cfg["bg"][0]]
        frame[:] = bg
        y1=max(0,fs); y2=min(self.canvas_h, fs+self.video_h)
        if y1<y2:
            crop=self.canvas_np[y1:y2]; dst=y1-fs
            frame[dst:dst+crop.shape[0]] = crop

        # Active system info
        fn, mi = self._active_at(time_s)
        fidx   = cfg["file_nums"].index(fn)
        sd     = self.score_data[fn]
        n_s    = len(self.layouts[fn]["tops"])
        mps    = max(1, sd["n_measures"]//n_s)
        sys_i  = min(mi//mps, n_s-1)
        ct = self._sys_top(fn,fidx,sys_i)-fs
        cb = self._sys_bot(fn,fidx,sys_i)-fs
        pad=18; ht=max(0,int(ct)-pad); hb=min(self.video_h,int(cb)+pad)

        # ── Header fades in when second line starts playing ─────────────────
        HEADER_H = 38
        FADE_DUR = 0.8
        h_prog = max(0.0, min(1.0, (time_s - self._t_second_line) / FADE_DUR))

        if h_prog > 0.01 and cfg.get("show_header", True):
            # Build header as numpy array
            hdr = np.zeros((HEADER_H, self.video_w, 3), dtype=np.uint8)
            # Gradient: slightly lighter at bottom (dark charcoal → near-black)
            for row in range(HEADER_H):
                v = int(12 + 8*(row/HEADER_H))
                hdr[row] = [v, v, v]
            # Orange accent line at bottom
            hdr[-2:] = [0, 130, 255]   # BGR orange

            # Draw text via PIL on the header strip
            h_pil  = Image.fromarray(hdr[..., ::-1])   # BGR→RGB
            h_draw = ImageDraw.Draw(h_pil)
            W = self.video_w

            # Left section: "Página N" (22% width)
            left_w = int(W * 0.22)
            pg_text = f"Página {fn}"
            h_draw.text((16, (HEADER_H-17)//2), pg_text,
                        font=self._font_sm, fill=(160, 190, 220))

            # Right section: "N BPM" (22% width, right-aligned)
            right_w = int(W * 0.22)
            bpm_text = f"{cfg['bpm']} BPM"
            try:
                bw = h_draw.textlength(bpm_text, font=self._font_sm)
            except AttributeError:
                bw = len(bpm_text) * 10
            h_draw.text((W - bw - 16, (HEADER_H-17)//2), bpm_text,
                        font=self._font_sm, fill=(255, 190, 50))

            # Center: song name (truncate if too long)
            center_w = W - left_w - right_w
            cname = self.song_name
            try:
                while len(cname) > 4:
                    tw = h_draw.textlength(cname, font=self._font_lg)
                    if tw <= center_w - 20: break
                    cname = cname[:-1]
                if len(cname) < len(self.song_name): cname = cname.rstrip() + "…"
            except AttributeError:
                pass
            cx = left_w + center_w//2
            cy = HEADER_H//2
            h_draw.text((cx, cy), cname, font=self._font_lg,
                        fill=(255, 245, 210), anchor="mm")

            hdr = np.array(h_pil)[..., ::-1]  # RGB→BGR

            # Blend header into frame with fade alpha
            region = frame[:HEADER_H].astype(np.float32)
            hdr_f  = hdr.astype(np.float32)
            frame[:HEADER_H] = (region*(1-h_prog) + hdr_f*h_prog).astype(np.uint8)

        # ── Beat dots (PIL) ───────────────────────────────────────────────────
        frame_pil = Image.fromarray(frame[..., ::-1])
        draw = ImageDraw.Draw(frame_pil)
        spb = 60.0/cfg["bpm"]; spm = sd["beats_pm"]*spb
        beat = int((time_s%spm)/spb) % int(sd["beats_pm"])
        nb   = int(sd["beats_pm"]); dr,gap = 8,22
        x0 = (self.video_w-nb*gap)//2; cy2 = self.video_h-30
        for b in range(nb):
            cx3 = x0+b*gap+gap//2
            clr = (255,190,50) if b==beat else (55,55,55)
            draw.ellipse([(cx3-dr,cy2-dr),(cx3+dr,cy2+dr)], fill=clr)
        frame = np.array(frame_pil)[..., ::-1]

        # ── Vertical playhead line ────────────────────────────────────────────
        lay  = self.layouts[fn]; sv = self._svg_scale[fn]
        sl   = lay["left_x"]*sv; sr = lay["right_x"]*sv
        m_in = mi % mps
        mw   = (sr-sl)/mps
        prog = (time_s % spm) / spm
        vl_x = max(0, min(self.video_w-1, int(sl + m_in*mw + prog*mw)))
        eff_hdr = int(HEADER_H * h_prog) + 4
        vl_top  = max(eff_hdr, ht); vl_bot = min(self.video_h-1, hb)
        pw  = cfg["playhead_w"]; pc = cfg["playhead_color"]
        if vl_top < vl_bot:
            frame[vl_top:vl_bot, max(0,vl_x-pw):vl_x+pw] = [pc[2],pc[1],pc[0]]

        return frame


def build_engine(overrides=None):
    cfg = {**DEFAULT_CONFIG, **(overrides or {})}
    return ScoreEngine(cfg).build()

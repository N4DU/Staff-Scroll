"""progress.py — progreso profesional en la consola, por procedimiento.

Cada trabajo se divide en PROCEDIMIENTOS (fases). Cada fase tiene su propia
barra 0–100 % en la consola y, además, aporta un tramo del porcentaje global
que ve la barra de la página web. Si una fase falla, su barra queda marcada
en rojo con el error exacto y las instrucciones para reportarlo — así siempre
se sabe EXACTAMENTE en qué paso se detuvo el programa.

════════════════════════════════════════════════════════════════════════════
CÓMO AGREGAR UN NUEVO PROCEDIMIENTO (leer antes de tocar el pipeline)
════════════════════════════════════════════════════════════════════════════
1. Conseguí el objeto `Progress` del trabajo (en app.py se crea uno por job
   y se pasa a las funciones del pipeline como `progress=`).

2. Envolvé tu paso en un `with`:

       with progress.phase("Mi nuevo paso", span=(40, 55)) as ph:
           for i in range(n):
               trabajo(i)
               ph.update((i + 1) / n, f"elemento {i + 1}/{n}")

   • label  : nombre visible en la consola (en español, claro y corto).
   • span   : tramo (desde%, hasta%) del progreso GLOBAL de la web que ocupa
              este paso. Elegí un tramo proporcional a lo que tarda y ajustá
              los tramos vecinos para que no se solapen. Si el paso no debe
              mover la barra de la web (p. ej. corre en segundo plano),
              pasá span=None.
   • update(frac, detalle): frac va de 0.0 a 1.0 DENTRO del paso. El detalle
              es opcional y se muestra al lado de la barra.

3. No hace falta nada más: al salir del `with` la barra se marca ✓ con su
   duración; si el bloque lanza una excepción, se marca ✗ en rojo con el
   mensaje y la excepción sigue propagándose (el manejo de errores de la
   web no cambia).

Si tu paso corre en OTRO hilo (como el render en segundo plano), funciona
igual: el renderizador de consola es thread-safe y puede dibujar varias
barras activas a la vez.
════════════════════════════════════════════════════════════════════════════
"""
import os
import shutil
import sys
import time
import threading
import traceback

ISSUES_URL = "https://github.com/N4DU/Staff-Scroll/issues"

# En Windows, si stdout no acepta UTF-8 (salida redirigida a archivo, consolas
# con code page cp1252/cp437), imprimir █✓♪ lanzaría UnicodeEncodeError y
# tumbaría el programa. Con errors="replace" el carácter no representable se
# imprime como "?" y todo sigue andando.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

# ─── soporte ANSI ─────────────────────────────────────────────────────────────

def _is_tty():
    """¿Podemos reescribir la línea actual con '\\r'? (funciona en cualquier
    terminal interactiva: CMD, PowerShell, Windows Terminal, git-bash…)"""
    return (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
            and os.environ.get("TERM") != "dumb")


def _enable_colors():
    """Devuelve True si es seguro emitir colores ANSI. Solo colores: el
    dibujado en vivo usa únicamente '\\r', que funciona en todas partes
    (git-bash/MINGW ignora los códigos de mover el cursor y rompía las
    barras — por eso nunca se usan)."""
    if not _is_tty():
        return False
    if os.name == "nt":
        # Consolas tipo mintty (git-bash) entienden colores de por sí.
        if os.environ.get("MSYSTEM") or os.environ.get("WT_SESSION"):
            return True
        # Consola clásica de Windows: activar el procesamiento VT.
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if not k32.GetConsoleMode(h, ctypes.byref(mode)):
                return False
            return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
        except Exception:
            return False
    return True


def _term_width():
    try:
        w = shutil.get_terminal_size(fallback=(90, 24)).columns
    except Exception:
        w = 90
    return max(40, min(w, 120))


class _Ansi:
    def __init__(self, on):
        f = (lambda s: s) if on else (lambda s: "")
        self.cyan, self.green, self.red = f("\x1b[36m"), f("\x1b[32m"), f("\x1b[31m")
        self.dim, self.bold, self.off   = f("\x1b[2m"), f("\x1b[1m"), f("\x1b[0m")


# ─── renderizador de consola (compartido por todos los trabajos) ─────────────

class _Console:
    """UNA línea viva que se reescribe con '\\r' (compatible con cualquier
    terminal, incluida git-bash/MINGW, y con ventanas chicas: todo se recorta
    al ancho real). Al terminar cada fase queda una línea permanente con ✓/✗.
    En salidas que no son una terminal (pipes, logs) imprime hitos de 25 %."""

    def __init__(self):
        self._lock = threading.RLock()
        self._live = _is_tty()
        self.c = _Ansi(_enable_colors())
        self._active = []        # fases en curso (puede haber más de una)
        self._live_len = 0       # ancho visible de la línea viva actual
        self._last_draw = 0.0

    # — API interna que usan las fases —

    def start(self, phase):
        with self._lock:
            self._active.append(phase)
            if self._live:
                self._draw_live(force=True)
            else:
                print(f"▶ {phase.label}…", flush=True)

    def update(self, phase):
        with self._lock:
            if self._live:
                self._draw_live()
            else:
                # hito de 25 %: una línea por cuarto, sin inundar la consola
                q = int(phase.frac * 4)
                if q > phase._last_quarter and q < 4:
                    phase._last_quarter = q
                    d = f" · {phase.detail}" if phase.detail else ""
                    print(self._fit(f"   {phase.label} — {int(phase.frac*100)}%{d}"),
                          flush=True)

    def finish(self, phase, error=None):
        with self._lock:
            if phase in self._active:
                self._active.remove(phase)
            self._clear_live()
            print(self._final_line(phase, error), flush=True)
            if error is not None:
                self._print_error(phase, error)
            if self._live:
                self._draw_live(force=True)

    def cancel(self, phase, note=""):
        """Cierre neutro (ni ✓ ni ✗): la fase se abandonó a propósito."""
        with self._lock:
            if phase in self._active:
                self._active.remove(phase)
            self._clear_live()
            c = self.c
            extra = f" — {note}" if note else ""
            print(self._fit(f"  {c.dim}◌ {phase.label} — cancelado{extra}{c.off}",
                            _term_width() + len(c.dim) + len(c.off)), flush=True)
            if self._live:
                self._draw_live(force=True)

    def println(self, text=""):
        """Imprime una línea permanente sin romper la línea viva."""
        with self._lock:
            self._clear_live()
            print(text, flush=True)
            if self._live:
                self._draw_live(force=True)

    # — dibujo —

    @staticmethod
    def _bar(frac, width):
        filled = int(round(frac * width))
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def _fit(text, width=None):
        width = width or _term_width()
        return text if len(text) <= width - 1 else text[:width - 2] + "…"

    def _live_line(self, ph, width):
        # "  Nombre del paso  ████░░░░░░  45% · detalle"  — recortado al ancho
        c = self.c
        extra = f"  (+{len(self._active) - 1} en curso)" if len(self._active) > 1 else ""
        bar_w = 18 if width >= 84 else (12 if width >= 64 else 8)
        pct = f"{int(ph.frac * 100):3d}%"
        fixed = 2 + bar_w + 2 + len(pct) + len(extra)      # todo menos label/detalle
        lab_w = max(8, width - 1 - fixed - 12)
        label = ph.label if len(ph.label) <= lab_w else ph.label[:lab_w - 1] + "…"
        room = width - 1 - fixed - len(label) - 2
        detail = ""
        if ph.detail and room > 4:
            detail = f" · {ph.detail}"
            if len(detail) > room:
                detail = detail[:room - 1] + "…"
        plain = f"  {label} {self._bar(ph.frac, bar_w)} {pct}{detail}{extra}"
        colored = (f"  {label} {c.cyan}{self._bar(ph.frac, bar_w)}{c.off} "
                   f"{pct}{c.dim}{detail}{extra}{c.off}")
        return plain, colored

    def _final_line(self, ph, error):
        c = self.c
        secs = f"{ph.elapsed:.1f}s"
        if error is None:
            bar = f" {self._bar(1.0, 10)} " if _term_width() >= 64 else " — "
            return self._fit(f"  {c.green}✓ {ph.label}{c.off}{c.green}{bar}{c.off}"
                             f"{c.dim}{secs}{c.off}",
                             _term_width() + (len(c.green) * 3 + len(c.off) * 3
                                              + len(c.dim)))
        return self._fit(f"  {c.red}✗ {ph.label} — FALLÓ a los {secs} "
                         f"({int(ph.frac * 100)}%){c.off}",
                         _term_width() + len(c.red) + len(c.off))

    def _print_error(self, ph, error):
        c = self.c
        print(f"    {c.red}{c.bold}Error:{c.off} {error}", flush=True)
        tb = traceback.format_exc()
        if tb and "NoneType: None" not in tb:
            last = tb.strip().splitlines()
            src = next((l.strip() for l in reversed(last) if l.strip().startswith("File ")), "")
            if src:
                print(f"    {c.dim}{src}{c.off}", flush=True)
        print(f"    {c.dim}Si el problema persiste, repórtalo (copiando estas "
              f"líneas) en:{c.off} {ISSUES_URL}", flush=True)

    def _clear_live(self):
        if self._live and self._live_len:
            sys.stdout.write("\r" + " " * self._live_len + "\r")
            sys.stdout.flush()
            self._live_len = 0

    def _draw_live(self, force=False):
        if not self._active:
            self._clear_live()
            return
        now = time.monotonic()
        if not force and now - self._last_draw < 0.08:   # ~12 dibujos/s máx.
            return
        self._last_draw = now
        # se muestra la fase actualizada más recientemente; si hay más de una
        # en curso, la línea lo indica con "(+N en curso)"
        ph = max(self._active, key=lambda p: p._last_update)
        plain, colored = self._live_line(ph, _term_width())
        pad = max(0, self._live_len - len(plain))
        sys.stdout.write("\r" + colored + " " * pad + "\b" * pad)
        sys.stdout.flush()
        self._live_len = len(plain)


CONSOLE = _Console()


# ─── API pública ──────────────────────────────────────────────────────────────

class Phase:
    """Un procedimiento con barra propia. Crear siempre vía Progress.phase()."""

    def __init__(self, progress, label, span):
        self._progress = progress
        self.label = label
        self.span = span
        self.frac = 0.0
        self.detail = ""
        self._t0 = time.monotonic()
        self._last_update = self._t0
        self._last_quarter = 0
        self._finished = False

    @property
    def elapsed(self):
        return time.monotonic() - self._t0

    def update(self, frac, detail=""):
        self.frac = min(1.0, max(0.0, float(frac)))
        if detail:
            self.detail = str(detail)
        self._last_update = time.monotonic()
        CONSOLE.update(self)
        if self.span and self._progress.web_cb:
            a, b = self.span
            msg = self.label + (f" — {self.detail}" if self.detail else "") + "…"
            self._progress.web_cb(int(round(a + self.frac * (b - a))), msg)

    def cancel(self, note=""):
        """Abandona la fase a propósito (sin ✓ ni ✗). Útil cuando el trabajo
        de la fase se descarta — p. ej. un render que se reemplaza por otro."""
        if self._finished:
            return
        self._finished = True
        CONSOLE.cancel(self, note)

    def __enter__(self):
        CONSOLE.start(self)
        self.update(0.0)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._finished:
            return False
        self._finished = True
        if exc is None:
            self.frac = 1.0
            CONSOLE.finish(self)
            if self.span and self._progress.web_cb:
                self._progress.web_cb(self.span[1], self.label + " ✓")
        else:
            CONSOLE.finish(self, error=exc)
            self._progress._failed = True
        return False   # nunca tragarse la excepción


class Progress:
    """Progreso de UN trabajo. `web_cb(pct, msg)` alimenta la barra de la web
    (puede ser None para trabajos sin interfaz, p. ej. tests)."""

    def __init__(self, web_cb=None, tag=""):
        self.web_cb = web_cb
        self.tag = tag
        self._failed = False

    def reset(self):
        """Olvida fallos anteriores (para reintentos sobre el mismo trabajo)."""
        self._failed = False

    def phase(self, label, span=None):
        if self.tag:
            label = f"[{self.tag}] {label}"
        return Phase(self, label, span)

    def announce(self, text):
        """Encabezado permanente en la consola (inicio de trabajo, resumen…)."""
        c = CONSOLE.c
        CONSOLE.println(f"{c.bold}{text}{c.off}")

    def error(self, exc):
        """Error ocurrido FUERA de una fase (o resumen final del fallo)."""
        if self._failed:
            return   # la fase que falló ya lo reportó con detalle
        c = CONSOLE.c
        CONSOLE.println(f"  {c.red}✗ Error: {exc}{c.off}")
        CONSOLE.println(f"    {c.dim}Si el problema persiste, repórtalo en:{c.off} "
                        f"{ISSUES_URL}")

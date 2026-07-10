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
import sys
import time
import threading
import traceback

ISSUES_URL = "https://github.com/N4DU/Staff-Scroll/issues"

_BAR_W   = 26          # ancho de la barra en caracteres
_LABEL_W = 34          # ancho reservado para el nombre del paso

# ─── soporte ANSI ─────────────────────────────────────────────────────────────

def _enable_ansi():
    """Devuelve True si la consola acepta códigos ANSI (barras en vivo)."""
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.name == "nt":
        # Activar el procesamiento de secuencias VT en la consola de Windows.
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


class _Ansi:
    def __init__(self, on):
        f = (lambda s: s) if on else (lambda s: "")
        self.cyan, self.green, self.red = f("\x1b[36m"), f("\x1b[32m"), f("\x1b[31m")
        self.dim, self.bold, self.off   = f("\x1b[2m"), f("\x1b[1m"), f("\x1b[0m")


# ─── renderizador de consola (compartido por todos los trabajos) ─────────────

class _Console:
    """Dibuja las barras activas en bloque, redibujando en el lugar (ANSI).
    En consolas sin ANSI (p. ej. git-bash/MINGW) imprime líneas de avance en
    los hitos de 25 % — sin basura de códigos de escape."""

    def __init__(self):
        self._lock = threading.RLock()
        self._live = _enable_ansi()
        self.c = _Ansi(self._live)
        self._active = []        # fases visibles en el bloque en vivo
        self._n_drawn = 0        # líneas del bloque actualmente en pantalla
        self._last_draw = 0.0

    # — API interna que usan las fases —

    def start(self, phase):
        with self._lock:
            self._active.append(phase)
            if self._live:
                self._redraw(force=True)
            else:
                print(f"▶ {phase.label}…", flush=True)

    def update(self, phase):
        with self._lock:
            if self._live:
                self._redraw()
            else:
                # hito de 25 %: una línea por cuarto, sin inundar la consola
                q = int(phase.frac * 4)
                if q > phase._last_quarter and q < 4:
                    phase._last_quarter = q
                    d = f"  ({phase.detail})" if phase.detail else ""
                    print(f"   {phase.label} — {int(phase.frac * 100)}%{d}", flush=True)

    def finish(self, phase, error=None):
        with self._lock:
            if phase in self._active:
                self._active.remove(phase)
            line = self._final_line(phase, error)
            if self._live:
                self._clear_block()
                print(line, flush=True)
                self._redraw(force=True)
            else:
                print(line, flush=True)
            if error is not None:
                self._print_error(phase, error)

    def println(self, text=""):
        """Imprime una línea permanente sin romper el bloque de barras."""
        with self._lock:
            if self._live:
                self._clear_block()
                print(text, flush=True)
                self._redraw(force=True)
            else:
                print(text, flush=True)

    # — dibujo —

    def _bar(self, frac):
        filled = int(round(frac * _BAR_W))
        return "█" * filled + "─" * (_BAR_W - filled)

    def _line(self, ph):
        c = self.c
        pct = f"{int(ph.frac * 100):3d}%"
        detail = f"  {c.dim}{ph.detail}{c.off}" if ph.detail else ""
        return (f"  {ph.label[:_LABEL_W].ljust(_LABEL_W)} "
                f"{c.cyan}{self._bar(ph.frac)}{c.off} {pct}{detail}")

    def _final_line(self, ph, error):
        c = self.c
        secs = f"{ph.elapsed:.1f}s"
        if error is None:
            return (f"  {c.green}✓{c.off} {ph.label[:_LABEL_W].ljust(_LABEL_W)} "
                    f"{c.green}{self._bar(1.0)}{c.off} 100%  {c.dim}{secs}{c.off}")
        return (f"  {c.red}✗ {ph.label} — FALLÓ a los {secs} "
                f"({int(ph.frac * 100)}%){c.off}")

    def _print_error(self, ph, error):
        c = self.c
        print(f"    {c.red}{c.bold}Error:{c.off} {error}", flush=True)
        tb = traceback.format_exc()
        if tb and "NoneType: None" not in tb:
            last = tb.strip().splitlines()
            src = next((l.strip() for l in reversed(last) if l.strip().startswith("File ")), "")
            if src:
                print(f"    {c.dim}{src}{c.off}", flush=True)
        print(f"    {c.dim}Si el problema persiste, reportalo (copiando estas "
              f"líneas) en:{c.off} {ISSUES_URL}", flush=True)

    def _clear_block(self):
        if self._n_drawn:
            # subir al comienzo del bloque y borrar hasta el final de pantalla
            sys.stdout.write(f"\x1b[{self._n_drawn}F\x1b[0J")
            self._n_drawn = 0

    def _redraw(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_draw < 0.08:   # ~12 fps máx.
            return
        self._last_draw = now
        self._clear_block()
        for ph in self._active:
            sys.stdout.write(self._line(ph) + "\n")
        self._n_drawn = len(self._active)
        sys.stdout.flush()


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
        self._last_quarter = 0
        self._finished = False

    @property
    def elapsed(self):
        return time.monotonic() - self._t0

    def update(self, frac, detail=""):
        self.frac = min(1.0, max(0.0, float(frac)))
        if detail:
            self.detail = str(detail)
        CONSOLE.update(self)
        if self.span and self._progress.web_cb:
            a, b = self.span
            msg = self.label + (f" — {self.detail}" if self.detail else "") + "…"
            self._progress.web_cb(int(round(a + self.frac * (b - a))), msg)

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
        CONSOLE.println(f"    {c.dim}Si el problema persiste, reportalo en:{c.off} "
                        f"{ISSUES_URL}")

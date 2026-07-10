import logging
import threading
import time
import webbrowser

import flask.cli

from app import create_app
from progress import CONSOLE

PORT = 5173


def _open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{PORT}")


def _banner():
    c = CONSOLE.c
    line = "─" * 52
    print(line)
    print(f"  {c.bold}♪ Scrolling Score{c.off}")
    print(f"  Servidor listo → {c.cyan}http://localhost:{PORT}{c.off}")
    print(f"  {c.dim}El progreso de cada trabajo se muestra debajo,")
    print(f"  paso por paso. Ctrl+C para salir.{c.off}")
    print(line, flush=True)


if __name__ == "__main__":
    # Consola profesional: sin el aviso de "development server" ni una línea
    # por cada petición HTTP. Los errores reales del servidor SÍ se muestran
    # (nivel ERROR), y cada procedimiento de un trabajo dibuja su propia
    # barra de progreso (ver progress.py).
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask.cli.show_server_banner = lambda *a, **k: None

    _banner()
    threading.Thread(target=_open_browser, daemon=True).start()
    app = create_app()
    # threaded=True: el editor reproduce video y audio en paralelo (peticiones
    # Range simultáneas) — con un solo hilo el preview se congela.
    app.run(port=PORT, debug=False, use_reloader=False, threaded=True)

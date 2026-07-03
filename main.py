import webbrowser
import threading
import time
from app import create_app

app = create_app()


def _open_browser():
    time.sleep(1.2)
    webbrowser.open("http://localhost:5173")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(port=5173, debug=False, use_reloader=False)

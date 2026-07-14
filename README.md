# ♪ Scrolling Score

Convierte partituras de batería de **MuseScore** en un **video con scroll**
sincronizado al audio real de la canción, con un **editor de sincronización**
que corre en el navegador.

Subís las hojas de la partitura (`.mscz`) y el audio de la canción; la app las
renderiza con MuseScore, detecta los golpes del audio y arma un video donde una
línea lectora recorre la partitura al ritmo de la música. Antes de exportar,
podés ajustar la sincronización a mano en un editor visual.

---

## Requisitos

- **Python 3.10+**
- **MuseScore 3 o 4** instalado (la app lo busca en las rutas estándar; en
  Windows también podés dejarlo en `vendor/`; ver `vendor/README.txt`).
- **ffmpeg** en el `PATH` (en Windows podés dejar `vendor/ffmpeg.exe`).

Dependencias de Python:

```bash
pip install -r requirements.txt
```

## Cómo se usa

```bash
python main.py
```

Se abre solo el navegador en **http://localhost:5173**. Desde ahí:

1. Subís las hojas de la partitura en `.mscz` (una hoja por archivo, o un
   archivo con varias páginas) y el audio de la canción.
2. La app renderiza las hojas y analiza el audio.
3. En el **editor** alineás la partitura con la canción (arrastrar las tiras,
   clic en un compás para rebobinar, modo `[D]` para afinar pulsos).
4. Exportás el video `.mp4` sincronizado.

> Está pensado para **hojas de un solo pentagrama** (como las de batería). Las
> partituras multi-pentagrama (piano, cuarteto, coro) no están soportadas y se
> rechazan con un aviso claro.

## Estructura del proyecto

| Archivo | Qué hace |
|---|---|
| `main.py` | Punto de entrada: levanta el servidor y abre el navegador. |
| `app.py` | Servidor Flask: subida, trabajos, progreso (SSE) y editor. |
| `score_engine.py` | Motor: parseo de la partitura, geometría, keyframes y render de cada frame. |
| `musescore_pipeline.py` | Llama a MuseScore para exportar PNG/SVG de cada hoja. |
| `audio_sync.py` | Análisis del audio (detección de golpes / envolvente de onda). |
| `progress.py` | Barras de progreso en consola. |
| `templates/index.html` | Toda la interfaz web (subida + editor de sincronización). |
| `vendor/` | Binarios opcionales para empaquetar en Windows (`ffmpeg.exe`). |
| `build.spec` | Config de PyInstaller para generar el `.exe`. |

## Empaquetar (opcional)

Para generar un ejecutable con PyInstaller:

```bash
pyinstaller build.spec
```

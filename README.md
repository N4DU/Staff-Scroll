<p align="center">
  <img src="docs/banner.png" alt="Project by N4DU — with Fable 5" width="720">
</p>

<h1 align="center">♪ Scrolling Score</h1>

<p align="center">
  Convierte partituras de batería de <b>MuseScore</b> en <b>videos con scroll
  sincronizados al audio real</b> de la canción — con un editor visual para
  clavar la sincronización golpe a golpe.
</p>

<p align="center">
  <a href="LICENSE"><img alt="Licencia AGPL-3.0" src="https://img.shields.io/badge/licencia-AGPL--3.0-orange"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="MuseScore 3 / 4" src="https://img.shields.io/badge/MuseScore-3%20%7C%204-lightgrey">
</p>

---

> **In English:** Scrolling Score turns **MuseScore drum sheet music** (`.mscz`)
> into a **scrolling sheet-music video synchronized with the real song audio** —
> a play-along / practice video where a playhead follows the score in time with
> the recording. It includes a browser-based sync editor (beat-accurate
> alignment, per-note fine-tuning) and exports MP4. Local Flask app; AGPL-3.0.

## ¿Qué hace?

Practicar batería leyendo partitura mientras suena la canción real es
incómodo: hay que pasar páginas, no se sabe en qué compás va la música, y los
videos de "sheet music" hechos a mano nunca quedan bien sincronizados.

Scrolling Score lo automatiza: se cargan las hojas (`.mscz`) y el audio, la
aplicación renderiza la partitura con MuseScore, analiza los golpes de la
canción y produce un **MP4** donde una línea lectora recorre la partitura al
ritmo exacto de la grabación. Antes de exportar, un **editor de
sincronización** permite alinear y afinar todo a mano.

| Pantalla de inicio | Editor de sincronización |
|---|---|
| ![Pantalla de inicio](docs/captura-inicio.png) | ![Editor de sincronización](docs/captura-editor.png) |

### Características

- **Sincronización golpe a golpe** — la posición de cada ataque se lee del
  engraving real de MuseScore (SVG), no de un reparto uniforme del compás.
- **Editor visual en el navegador** — dos tiras de tiempo (partitura y
  canción) para alinear arrastrando, con forma de onda de alta resolución,
  imán a los pulsos y reproducción instantánea (audio pre-decodificado).
- **Modo diagnóstico `[D]`** — muestra compases y pulsos detectados sobre la
  hoja y permite corregir la posición de cualquier pulso individual.
- **Proyectos `.sscroll`** — todo el trabajo (hojas, audio, configuración y
  sincronización) viaja en un único archivo reabrible y compartible.
- **Configurable** — resolución (hasta 4K), estilo y posición de la línea
  lectora (continua o por tiempos), conteo previo, recorte entre páginas,
  perfiles de configuración con nombre y valores predeterminados propios.
- **Soporta** repeticiones y casillas (voltas), cambios de tempo y de compás,
  métricas compuestas (6/8…), tresillos, archivos de una o varias hojas, y
  partituras de MuseScore 2, 3 y 4.

## Requisitos

| Requisito | Detalle |
|---|---|
| **Python 3.10+** | con `pip install -r requirements.txt` (Flask, NumPy, Pillow) |
| **MuseScore 3 o 4** | se busca en las rutas estándar; en Windows también puede dejarse en `vendor/` (ver `vendor/README.txt`) |
| **ffmpeg** | en el `PATH`; en Windows puede dejarse en `vendor/ffmpeg.exe` |

## Uso

```bash
python main.py
```

El navegador se abre solo en **http://localhost:5173**.

1. **Cargar** las hojas `.mscz` (una por archivo o un archivo multi-hoja; se
   reordenan arrastrando) y el audio de la canción (mp3, wav, m4a, ogg, flac…).
2. **Configurar** lo que haga falta (resolución, línea lectora, conteo…) — o
   nada: los valores por defecto funcionan.
3. **Generar** — la aplicación renderiza y abre el **editor de
   sincronización**.
4. **Sincronizar**: arrastrar las bandas para alinear partitura y canción,
   verificar con ▶ (y «Probar el final»), afinar pulsos con `[D]` si hace
   falta.
5. **✓ Listo** → «Generar video con audio» — y descargar el MP4 (junto con el
   proyecto `.sscroll`, si la casilla queda marcada).

### Atajos del editor

| Acción | Cómo |
|---|---|
| Alinear partitura ↔ canción | arrastrar la banda superior (partitura) o inferior (canción) |
| Mover el cursor | arrastrar la línea blanca, o clic en las tiras |
| Rebobinar a un compás | clic sobre ese compás en la hoja |
| Reproducir / pausar | `Espacio` |
| Retroceder / avanzar un pulso | `←` / `→` (con `Shift`: afinar la canción ±10 ms) |
| Zoom en las tiras | rueda del ratón |
| Diagnóstico y corrección de pulsos | `D`, luego seleccionar un pulso y «✏ Corregir» |

## Proyectos (`.sscroll`)

Al terminar, la casilla **«Guardar también el proyecto»** (siempre marcada)
descarga junto al video un único archivo `.sscroll` con **todo**: las hojas,
el audio, la configuración, la alineación y las correcciones de pulsos.

Para retomar el trabajo — o continuarlo en **otra computadora** — basta
arrastrar ese archivo a la pantalla de inicio: la aplicación muestra qué
contiene, permite ajustar la configuración (por ejemplo la resolución) y abre
el editor **tal cual quedó al guardarse**. La sincronización es independiente
de la resolución.

El formato está **definido y congelado** en
[`docs/FORMATO_SSCROLL.md`](docs/FORMATO_SSCROLL.md): los proyectos guardados
hoy podrán abrirse siempre, sin importar cuánto evolucione la aplicación.

## Limitaciones

- Pensado para **hojas de un solo pentagrama** (como las de batería). Las
  partituras multi-pentagrama (piano, cuarteto, coro) se rechazan con un
  aviso claro.
- La aplicación corre **localmente** (no es un servicio web público): un
  usuario a la vez.

## Estructura del proyecto

| Archivo | Qué hace |
|---|---|
| `main.py` | Punto de entrada: levanta el servidor y abre el navegador. |
| `app.py` | Servidor Flask: subida, trabajos, progreso (SSE), proyectos y editor. |
| `score_engine.py` | Motor: parseo de la partitura, geometría, keyframes y render de cada frame. |
| `musescore_pipeline.py` | Llama a MuseScore para exportar PNG/SVG de cada hoja. |
| `audio_sync.py` | Análisis del audio (detección de golpes y envolvente de onda). |
| `progress.py` | Barras de progreso en consola. |
| `templates/index.html` | Toda la interfaz web (inicio + editor de sincronización). |
| `docs/` | Banner, capturas y la especificación del formato `.sscroll`. |
| `vendor/` | Binarios opcionales para empaquetar en Windows (`ffmpeg.exe`). |
| `build.spec` | Configuración de PyInstaller para generar el `.exe`. |

Para generar un ejecutable de Windows: `pyinstaller build.spec`.

## Licencia

Copyright © 2026 **N4DU**

Scrolling Score es software libre bajo la **GNU Affero General Public License
v3.0 (AGPL-3.0)** — ver [`LICENSE`](LICENSE). Puede usarse, estudiarse,
modificarse y compartirse libremente; pero quien distribuya una versión
modificada **o la ofrezca como servicio en red** debe publicar su código
fuente bajo esta misma licencia. Se entrega sin ninguna garantía.

> `vendor/ffmpeg.exe` se distribuye bajo su propia licencia
> ([FFmpeg](https://ffmpeg.org/legal.html), LGPL/GPL), independiente de la de
> este proyecto. MuseScore es una aplicación externa que el usuario instala
> por su cuenta.

# Formato de proyecto `.sscroll` — especificación (formato 1)

Este documento **define y congela** el formato de los archivos de proyecto de
Scrolling Score. Es un contrato: las versiones futuras de la aplicación pueden
agregar funciones, pero **todo proyecto guardado con el formato 1 debe poder
abrirse siempre**. Cualquier cambio al formato debe respetar las reglas de
compatibilidad del final de este documento.

## Contenedor

Un `.sscroll` es un archivo **ZIP** estándar (con compresión Deflate) que
contiene, en estas rutas exactas:

| Miembro | Obligatorio | Contenido |
|---|---|---|
| `project.json` | **Sí** | Metadatos del proyecto (UTF-8, ver abajo). |
| `partituras/*.mscz` | **Sí** (≥ 1) | Las hojas de la partitura, tal cual se subieron. El **orden de las páginas** es el orden alfabético de los nombres (llevan el prefijo `NNN-` que asigna la aplicación al subir). |
| `audio/song.*` | No | El audio de la canción, tal cual se subió (una sola pista; la extensión conserva el formato original). |
| `thumbnail.png` | No | Miniatura de cortesía (primera hoja). Quien lee el archivo **no debe depender** de su presencia. |

Cualquier otro miembro del ZIP debe **ignorarse** al leer.

## `project.json`

Objeto JSON con estos campos:

| Campo | Tipo | Unidad / rango | Significado |
|---|---|---|---|
| `app` | string | — | Siempre `"Scrolling Score"` (identificación de cortesía). |
| `format` | entero | ≥ 1 | Versión del formato. **Este documento define el formato `1`.** |
| `song_name` | string | — | Nombre de la canción (puede ser vacío). |
| `offset` | número | segundos, `[-7200, 7200]` | Alineación: dónde cae el compás 1 respecto del inicio de la canción (positivo = el compás 1 suena después de que empieza el audio). **Independiente de la resolución.** |
| `pulse_fixes` | lista | — | Correcciones de pulsos hechas en el modo [D]. Cada elemento: `{"i": entero, "k": entero, "x": número}` donde `i` es el índice del compás en la línea de tiempo, `k` el índice del pulso dentro del compás y `x` la posición horizontal como **fracción del ancho de la hoja** `[0, 1]`. **Independiente de la resolución.** |
| `settings` | objeto | — | La configuración de la interfaz, con el mismo esquema que los perfiles (`FACTORY` en `templates/index.html`): `header`, `songName`, `playhead`, `pageGap`, `countIn`, `frac`, `phMode`, `phColor`, `phAlpha`, `phWidth`, `resPreset`, `resW`, `resH`. |

### Ejemplo

```json
{
 "app": "Scrolling Score",
 "format": 1,
 "settings": {"header": true, "songName": "That Band", "playhead": true,
              "pageGap": 30, "countIn": 4, "frac": 50, "phMode": "fluid",
              "phColor": "#ff9b00", "phAlpha": 100, "phWidth": 3,
              "resPreset": "auto", "resW": 1920, "resH": 1080},
 "offset": -3.25,
 "pulse_fixes": [{"i": 0, "k": 1, "x": 0.4242}],
 "song_name": "That Band"
}
```

## Reglas de compatibilidad (las importantes)

1. **Un lector debe ignorar los campos y miembros que no conozca.** Eso
   permite que versiones futuras *agreguen* información sin romper a los
   lectores viejos.
2. **Agregar campos u miembros opcionales NO cambia el número de formato.**
   `format` solo se incrementa ante un cambio **incompatible** (uno que un
   lector del formato 1 interpretaría mal). Debe evitarse siempre que exista
   una alternativa retrocompatible.
3. **Un lector debe rechazar con un mensaje claro** los archivos con
   `format` mayor al que entiende (pedir actualizar la aplicación), en lugar
   de abrirlos a medias.
4. **Los campos de sincronización son independientes de la resolución**
   (segundos y fracciones): un proyecto guardado en una computadora se abre
   idéntico en otra con distinta pantalla o resolución de exportación.
5. Ante campos ausentes en `settings`, el lector completa con los valores de
   fábrica (`FACTORY`); ante valores fuera de rango, los ajusta al rango.

> La cobertura de estas reglas está automatizada: el test estructural
> exhaustivo recorre cada clave del esquema de configuración y verifica el
> ciclo guardar → abrir → comparar, y el rechazo de formatos futuros.

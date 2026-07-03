vendor/
=======

ffmpeg.exe
----------
Colocá aquí el binario estático de ffmpeg para Windows.
Descargalo desde: https://www.gyan.dev/ffmpeg/builds/
  → ffmpeg-release-essentials.zip → bin/ffmpeg.exe

Sin ffmpeg.exe en esta carpeta, la app lo buscará en el PATH del sistema.
Si no está en ningún lado, la generación de video fallará con un error claro.


musescore_portable/  (opcional)
--------------------------------
Si querés incluir MuseScore portable en el .exe, extraé el instalador PAF
a una subcarpeta llamada "musescore_portable/" y asegurate de que el ejecutable
esté en:

  vendor\musescore_portable\MuseScore3.exe   (MuseScore 3)
  vendor\musescore_portable\MuseScore4.exe   (MuseScore 4)

Si no está aquí, la app lo buscará en las rutas de instalación estándar:
  C:\Program Files\MuseScore 4\bin\MuseScore4.exe
  C:\Program Files\MuseScore 3\bin\MuseScore3.exe

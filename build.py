"""Crea l'app installabile per il sistema operativo corrente.

    python build.py

Risultato in dist/: "Handball Video.app" su Mac, cartella "Handball Video" con l'.exe su Windows.
La build va fatta su ciascun sistema (o con la GitHub Action in .github/workflows/build.yml).
"""
import PyInstaller.__main__

from handballvideo import __app_name__

PyInstaller.__main__.run([
    "run.py",
    "--name", __app_name__,
    "--windowed",
    "--noconfirm",
    "--clean",
    "--collect-binaries", "imageio_ffmpeg",
    "--osx-bundle-identifier", "it.handballvideo.editor",
])

"""Módulo de música: búsqueda (yt-dlp), extracción y reproducción con FFmpeg en el propio servidor
(salida por el jack auxiliar o por un parlante Bluetooth del host).

Desacoplado del resto del asistente: no usa el modelo, la base de datos ni Redis. El único punto de
entrada para la API es `MusicController` (ver controller.py).
"""

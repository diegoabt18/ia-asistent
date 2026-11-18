"""Módulo de música: búsqueda (yt-dlp), extracción, transcodificación (FFmpeg) y stream a un ESP32.

Desacoplado del resto del asistente: no usa el modelo, la base de datos ni Redis. El único punto de
entrada para la API es `MusicController` (ver controller.py).
"""

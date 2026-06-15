#!/usr/bin/env python3

"""
Точка входа для запуска приложения ThongSSH.
Этот скрипт обеспечивает правильный запуск приложения как модуля,
чтобы все относительные импорты внутри пакета 'thongssh_gtk' работали корректно.
"""
import runpy
import os
import sys

# Force Wayland backend when Wayland is available, overriding any parent
# process (e.g. VSCode running in XWayland) that may have set GDK_BACKEND=x11.
if os.environ.get('WAYLAND_DISPLAY'):
    os.environ['GDK_BACKEND'] = 'wayland'

from gi.repository import Gio

# --- Загрузка ресурсов (если они не были загружены приложением) ---
try:
    # Находим путь к бинарному файлу ресурсов и загружаем его.
    resource_path = os.path.join(os.path.dirname(__file__), 'thongssh_gtk', 'thongssh.gresource')
    resource = Gio.Resource.load(resource_path)
    Gio.resources_register(resource)
except GLib.Error:
    # Ресурсы уже могут быть зарегистрированы, это нормально
    pass

runpy.run_module("thongssh_gtk.app", run_name="__main__")
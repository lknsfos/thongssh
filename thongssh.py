#!/usr/bin/env python3

"""
Entry point for running the ThongSSH application.
This script ensures the app is launched as a module so relative imports
inside the 'thongssh_gtk' package resolve correctly.
"""
import runpy
import os

# Force Wayland backend when Wayland is available, overriding any parent
# process (e.g. VSCode running in XWayland) that may have set GDK_BACKEND=x11.
if os.environ.get('WAYLAND_DISPLAY'):
    os.environ['GDK_BACKEND'] = 'wayland'

runpy.run_module("thongssh_gtk.app", run_name="__main__")
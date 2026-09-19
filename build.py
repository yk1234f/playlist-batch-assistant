"""Portable PyInstaller build, including Conda DLL search locations."""
from pathlib import Path
import os
import subprocess
import sys

root = Path(__file__).resolve().parent
command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
           '--name', 'PlaylistBatchAssistant', '--collect-all', 'mutagen', '--collect-all', 'playwright']
conda_bin = Path(sys.base_prefix) / 'Library' / 'bin'
if conda_bin.is_dir():
    os.environ['PATH'] = str(conda_bin) + os.pathsep + os.environ.get('PATH', '')
    for dll in ('tk86t.dll', 'tcl86t.dll', 'ffi.dll', 'libmpdec-4.dll'):
        source = conda_bin / dll
        if source.is_file():
            command.extend(['--add-binary', f'{source}{os.pathsep}.'])
command.append('app.py')
raise SystemExit(subprocess.call(command, cwd=root))

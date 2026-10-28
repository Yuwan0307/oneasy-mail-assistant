"""Small, shared helpers for desktop installers and services."""
import json
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from .config import load_config

VERSION = '1.6.8'


def service_settings(root):
    path = Path(root) / 'deployment.json'
    settings = {'service_name': 'OneasyMailAssistant', 'port': 8765}
    if path.exists():
        settings.update(json.loads(path.read_text(encoding='utf-8-sig')))
    name, port = settings['service_name'], settings['port']
    if not isinstance(name, str) or not re.fullmatch(r'OneasyMailAssistant[A-Za-z0-9_-]*', name):
        raise ValueError('Invalid Oneasy service name')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid local port')
    return settings


def copy_data(source, destination):
    """Take a consistent SQLite snapshot; do not overwrite a previous backup."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError('Backup destination already exists')
    if (source / 'config.json').exists():
        load_config(source / 'config.json')  # Check machine-bound encryption before migration.
    destination.mkdir(parents=True)
    for name in ('config.json', 'config.key'):
        if (source / name).exists():
            shutil.copy2(source / name, destination / name)
    database = source / 'main.db'
    if database.exists():
        with closing(sqlite3.connect(database)) as original, closing(sqlite3.connect(destination / 'main.db')) as backup:
            original.backup(backup)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == 'backup':
        copy_data(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit('Usage: python -m app.deployment backup SOURCE DESTINATION')

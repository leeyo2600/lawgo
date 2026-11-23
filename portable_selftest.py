"""Optional Windows self-test for bundled Python; no APIs or credentials used."""
from pathlib import Path
import os
import sys

ROOT=Path(__file__).resolve().parent
pyexe=ROOT/'runtime'/'python.exe'
print('Python:', sys.version)
print('Executable:', sys.executable)
assert sys.version_info[:2] == (3, 13), 'Python 3.13 expected'
assert Path(sys.executable).resolve() == pyexe.resolve(), 'Not running bundled python.exe'
for name in ('flask', 'waitress', 'requests', 'xlsxwriter', 'jinja2', 'werkzeug', 'ssl', 'ctypes', 'sqlite3'):
    mod=__import__(name)
    print('  import', name, 'OK')
import web_app
app=web_app.create_app()
response=app.test_client().get('/')
assert response.status_code == 200, f'Home returned HTTP {response.status_code}'
assert len(response.data) > 1000, 'Home page did not render'
print('Web page: HTTP 200')
print('All local checks passed. Remote API connectivity is not checked.')

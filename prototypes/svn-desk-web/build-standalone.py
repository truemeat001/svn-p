#!/usr/bin/env python3
"""Package the exact running web source as a double-clickable offline demo."""
from pathlib import Path
import base64
import re
root = Path(__file__).resolve().parent
web = root / 'web'
html = (web / 'index.html').read_text()
css = (web / 'styles.css').read_text()
js = '\n'.join((web / name).read_text() for name in ['model.js', 'demo-data.js', 'app.js'])
js = re.sub(r'^import .+?;\n', '', js, flags=re.MULTILINE)
js = re.sub(r'^export ', '', js, flags=re.MULTILINE)
# app.js aliases the model export. Imports disappear only in this offline build.
js = 'const esc = safeText;\n' + js
image = 'data:image/svg+xml;base64,' + base64.b64encode((web / 'favicon.svg').read_bytes()).decode()
html = html.replace('<link rel="stylesheet" href="./styles.css">', '<style>' + css + '</style>')
html = html.replace('./favicon.svg', image)
html = html.replace('<script type="module" src="./app.js"></script>', '<script type="module">' + js.replace('</script', '<\\/script') + '</script>')
(root / 'SVN-Desk-Web-Demo.html').write_text(html)
print('Built SVN-Desk-Web-Demo.html (offline demonstration, no API calls).')

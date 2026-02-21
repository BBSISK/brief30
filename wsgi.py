# WSGI configuration for PythonAnywhere
# ─────────────────────────────────────────────────────────────
# In PythonAnywhere Web tab:
#   Source code:     /home/<yourusername>/brief30
#   Working dir:     /home/<yourusername>/brief30
#   WSGI config:     point to this file (or paste into the PA editor)
#   Python version:  3.10+
#
# If using a virtualenv, set the path in the PA Web tab.
# Install dependencies: pip install flask
# ─────────────────────────────────────────────────────────────

import sys
import os

# Add the app folder to the Python path
path = os.path.dirname(__file__)
if path not in sys.path:
    sys.path.insert(0, path)

from app import app as application  # noqa

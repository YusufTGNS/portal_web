import sys
from pathlib import Path

import serverless_wsgi


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402


def handler(event, context):
    return serverless_wsgi.handle_request(app, event, context)

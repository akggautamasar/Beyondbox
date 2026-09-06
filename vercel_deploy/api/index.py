"""
Vercel entrypoint. Vercel's Python runtime looks for a module-level
ASGI/WSGI-compatible `app` object in a file under /api — it finds it here
and routes all requests to it directly (no Mangum/adapter needed).
"""
from terabox_api import app  # noqa: F401

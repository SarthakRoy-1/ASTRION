"""ASGI entry point.

    uvicorn app.backend.main:app --reload --port 8000

Kept to one line of real work so the application's assembly stays in
`app/backend/api/app.py` and is importable by tests without a server.
"""

from __future__ import annotations

from app.backend.api.app import create_app

app = create_app()

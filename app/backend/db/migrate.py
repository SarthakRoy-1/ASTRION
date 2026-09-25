"""`python -m app.backend.db.migrate` -- apply pending schema migrations.

Run by the deploy step before the API starts (see docker-entrypoint.sh), and by
hand. Reads `DATABASE_URL` from the environment like the API does, prints only
version numbers, and never prints the URL: it carries a password.
"""

from __future__ import annotations

import logging
import sys

from app.backend.core.config import load_settings
from app.backend.db import open_database


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    database = open_database(settings)
    if database.backend != "postgres":
        print("DATABASE_URL is not a PostgreSQL URL; nothing to migrate.")
        return 0
    try:
        applied = database.migrate()
    finally:
        database.close()
    if applied:
        print("applied migrations: " + ", ".join(applied))
    else:
        print("database is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())

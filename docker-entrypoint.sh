#!/bin/sh
# Bootstraps the database from the supplied source pack, then execs the
# real command (uvicorn).
#
# Runs ingestion only if the database file is absent, so a persisted volume
# survives container restarts (an executed action stays in place) while a
# fresh volume — or a fresh deployment — always builds correctly from
# data/source/ alone. The deployed backend never depends on a database file
# that exists only on the developer's machine.
set -eu

DB_PATH="${PARCELPILOT_DB_PATH:-data/processed/parcelpilot.db}"

if [ ! -f "$DB_PATH" ]; then
    echo "docker-entrypoint: $DB_PATH not found; ingesting from data/source/ ..."
    python scripts/ingest_dataset.py
    python scripts/ingest_documents.py
    echo "docker-entrypoint: ingestion complete."
else
    echo "docker-entrypoint: $DB_PATH already present; skipping ingestion."
fi

exec "$@"

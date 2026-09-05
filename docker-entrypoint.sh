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

# Derived from DATABASE_URL the same way app/backend/core/config.py parses
# it, so the file this script checks for and ingests into is guaranteed to be
# the same file the application connects to at request time. Passing --db
# explicitly (rather than letting the scripts fall back to their own
# DEFAULT_DB_PATH) is what keeps that guarantee: DATABASE_URL is the single
# source of truth for the database location, not two independently-defaulted
# paths that happen to agree only when nobody customises either one.
DB_PATH="${DATABASE_URL:-data/processed/parcelpilot.db}"
DB_PATH="${DB_PATH#sqlite:///}"
DB_PATH="${DB_PATH#sqlite://}"

if [ ! -f "$DB_PATH" ]; then
    echo "docker-entrypoint: $DB_PATH not found; ingesting from data/source/ ..."
    python scripts/ingest_dataset.py --db "$DB_PATH"
    python scripts/ingest_documents.py --db "$DB_PATH"
    echo "docker-entrypoint: ingestion complete."
else
    echo "docker-entrypoint: $DB_PATH already present; skipping ingestion."
fi

# The public demo tenant, seeded only when a deployment explicitly asks for it.
#
# Off unless DEMO_SEED_ENABLED is exactly "true": an unset variable, an empty
# one, or any other spelling leaves the database with no demo workspace at all.
# APP_ENV has no bearing on it -- a demo tenant is a deliberate choice an
# operator makes, never something a deployment inherits from calling itself
# production.
#
# Outside the ingestion branch above on purpose. The seed is idempotent, so
# running it on every boot is what lets an already-populated volume gain the
# demo tenant (and lets a half-finished first boot finish), while a database
# that already has it is left exactly as it is.
#
# `set -eu` makes a failure fatal, which is the intent: a container that
# started with a half-created demo environment would present a broken product
# to the one audience this feature exists for.
if [ "${DEMO_SEED_ENABLED:-false}" = "true" ]; then
    echo "docker-entrypoint: DEMO_SEED_ENABLED=true; seeding the demo workspace ..."
    python scripts/seed_demo.py --db "$DB_PATH"
    echo "docker-entrypoint: demo seed complete."
fi

exec "$@"

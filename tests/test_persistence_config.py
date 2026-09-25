"""What a production deployment may and may not persist to.

On a host with an ephemeral disk, SQLite and a local upload directory are not
slow storage, they are storage that disappears -- and everything appears to work
until it does. Configuration that would put data there must stop the process at
startup, where an operator reads the reason, not at 3am when the data is gone.
"""

from __future__ import annotations

import pytest

from app.backend.core.config import Settings, load_settings
from app.backend.core.errors import ConfigurationError

PG = "postgresql://user:pw@db.example.test:5432/astrion?sslmode=require"


def s3_settings(**over) -> Settings:
    base = dict(
        app_env="production",
        database_url=PG,
        storage_backend="s3",
        storage_bucket="astrion-documents",
        storage_endpoint_url="https://project.storage.supabase.co/storage/v1/s3",
        storage_region="ap-south-1",
        storage_access_key_id="AKIA-EXAMPLE",
        storage_secret_access_key="example-secret",
        demo_login_enabled=False,
    )
    base.update(over)
    return Settings(**base)


def test_production_with_postgres_and_object_storage_is_accepted():
    s3_settings().validate_persistence()


def test_production_refuses_sqlite():
    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        s3_settings(database_url="sqlite:///./data/processed/astrion.db").validate_persistence()
    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        s3_settings(database_url=None).validate_persistence()


def test_production_refuses_local_storage():
    with pytest.raises(ConfigurationError, match="object storage"):
        s3_settings(storage_backend="local").validate_persistence()


def test_development_may_use_sqlite_and_local_storage():
    Settings(app_env="development").validate_persistence()
    Settings(app_env="development", storage_backend="local").validate_persistence()


@pytest.mark.parametrize("missing", ["storage_bucket", "storage_access_key_id", "storage_secret_access_key"])
def test_an_s3_store_needs_its_bucket_and_credentials_even_outside_production(missing):
    with pytest.raises(ConfigurationError, match="STORAGE_"):
        s3_settings(app_env="development", **{missing: None}).validate_persistence()


def test_supabase_is_an_accepted_spelling_of_the_s3_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    assert load_settings().storage_backend == "s3"


def test_an_unknown_storage_backend_is_refused(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "floppy")
    with pytest.raises(ConfigurationError, match="STORAGE_BACKEND"):
        load_settings()


def test_the_demo_cannot_run_against_postgres():
    with pytest.raises(ConfigurationError, match="DEMO_LOGIN_ENABLED"):
        s3_settings(demo_login_enabled=True).validate_auth()


def test_credentials_never_appear_in_a_repr_or_the_public_summary():
    settings = s3_settings()
    shown = repr(settings) + str(settings.public_summary())
    for secret in ("example-secret", "AKIA-EXAMPLE", "user:pw", "db.example.test"):
        assert secret not in shown


def test_a_process_that_builds_its_own_settings_is_held_to_it(monkeypatch, tmp_path):
    """`create_app()` with no argument is the production entry point."""
    from app.backend.api.app import create_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.test")
    monkeypatch.setenv("DEMO_LOGIN_ENABLED", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    with pytest.raises(ConfigurationError):
        create_app()


def test_settings_a_test_passes_in_are_not_held_to_it(tmp_path):
    """The suite builds production-flavoured settings over SQLite on purpose."""
    from app.backend.api.app import create_app

    create_app(Settings(app_env="production", database_path=tmp_path / "x.db", cors_allow_origins=("https://a.example.test",), demo_login_enabled=False))

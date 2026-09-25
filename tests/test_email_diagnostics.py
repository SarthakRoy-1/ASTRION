"""A failed verification email must say why, and never say the code or the key.

In production a registration ended at "The email didn't send" while the log said
nothing: the no-provider path logged at DEBUG and a Resend rejection was reduced
to its class name. These pin what an operator can now read, and what may never
appear there.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from app.backend.core.config import DEFAULT_EMAIL_FROM, Settings
from app.backend.email.provider import EmailDeliveryError, NullEmailProvider
from app.backend.email.resend_provider import ResendEmailProvider, describe_failure


def production(**over) -> Settings:
    base = dict(app_env="production", require_verified_email=True, demo_login_enabled=False)
    base.update(over)
    return Settings(**base)


# --- the startup warning -------------------------------------------------------


def test_production_without_a_resend_key_warns_at_startup():
    (warning,) = production().email_configuration_warnings()
    assert "RESEND_API_KEY is not set" in warning


def test_a_key_with_the_placeholder_sender_warns_about_the_sender():
    (warning,) = production(resend_api_key="re_test").email_configuration_warnings()
    assert "EMAIL_FROM" in warning and DEFAULT_EMAIL_FROM in warning
    assert "re_test" not in warning


def test_a_complete_configuration_is_silent():
    settings = production(resend_api_key="re_test", email_from="Astrion <noreply@mail.example.test>")
    assert settings.email_configuration_warnings() == []


def test_no_warning_when_verification_is_not_required_or_outside_production():
    assert production(require_verified_email=False).email_configuration_warnings() == []
    assert Settings(app_env="development").email_configuration_warnings() == []


# --- the null provider ---------------------------------------------------------


def test_sending_with_no_provider_is_an_error_in_the_log(caplog):
    with caplog.at_level(logging.ERROR, logger="astrion.email"):
        with pytest.raises(EmailDeliveryError):
            NullEmailProvider().send_verification_code(
                to_address="a@example.test", display_name="A", code="123456", expires_minutes=10
            )
    assert "RESEND_API_KEY is not set" in caplog.text
    assert "123456" not in caplog.text and "a@example.test" not in caplog.text


# --- what a Resend rejection reports -------------------------------------------


class ResendError(Exception):
    """Shaped like the SDK's error, which carries the API response's fields."""

    __module__ = "resend.exceptions"

    def __init__(self, code, error_type, message):
        super().__init__(message)
        self.code, self.error_type, self.message = code, error_type, message


def test_the_providers_own_reason_is_reported():
    exc = ResendError(403, "validation_error", "The astrion.app domain is not verified.")
    described = describe_failure(exc)
    assert "ResendError" in described
    assert "code=403" in described
    assert "validation_error" in described
    assert "domain is not verified" in described


def test_digit_runs_are_masked_so_a_code_can_never_reach_the_log():
    described = describe_failure(ResendError(422, "validation_error", "bad body 482913 sent"))
    assert "482913" not in described


def test_a_non_provider_error_is_reported_by_class_name_only():
    class ConnectError(Exception):
        pass

    assert describe_failure(ConnectError("POST body: code=482913")) == "ConnectError"


def test_a_rejected_send_logs_the_reason_and_still_raises_a_clean_error(monkeypatch, caplog):
    def rejected(_params):
        raise ResendError(403, "validation_error", "The astrion.app domain is not verified.")

    fake = types.ModuleType("resend")
    fake.Emails = types.SimpleNamespace(send=rejected, SendParams=dict)
    monkeypatch.setitem(sys.modules, "resend", fake)

    provider = ResendEmailProvider(api_key="re_SECRET_KEY", from_address=DEFAULT_EMAIL_FROM)
    with caplog.at_level(logging.ERROR, logger="astrion.email.resend"):
        with pytest.raises(EmailDeliveryError) as raised:
            provider.send_verification_code(
                to_address="person@example.test", display_name="P", code="482913", expires_minutes=10
            )

    assert "domain is not verified" in caplog.text
    for secret in ("re_SECRET_KEY", "482913", "person@example.test"):
        assert secret not in caplog.text
        assert secret not in str(raised.value)

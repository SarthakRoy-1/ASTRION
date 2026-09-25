"""What "the code was sent" is allowed to mean.

In production a person was told "New code sent" while the email provider had been
asked for nothing. The interface may say a code was sent only when the provider
has accepted a message, so these hold every path to that: a real provider
answering yes, a real provider answering no, a provider that cannot be reached,
and the one case that used to answer yes without asking anyone, an address that
already has an account.
"""

from __future__ import annotations

import logging
import re
import sys
import types

import pytest
from fastapi.testclient import TestClient

from app.backend.email.provider import EmailDeliveryError
from app.backend.email.resend_provider import ResendEmailProvider, describe_failure
from app.backend.core.config import Settings
from test_auth_otp_oauth import (
    GOOD_PASSWORD,
    _client,
    _settings,
    make_user,
    register,
    verify,
)

FROM = "Astrion Verification <onboarding@resend.dev>"
OWNER = "owner@example.com"


class ResendError(Exception):
    """Shaped like the SDK's error, which carries the API response's fields."""

    __module__ = "resend.exceptions"

    def __init__(self, code, error_type, message):
        super().__init__(message)
        self.code, self.error_type, self.message = code, error_type, message


class FakeResend:
    """Stands in for the `resend` package: records every request it is asked to make."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.api_keys: list[str] = []
        self.behaviour = "accept"  # accept | reject | no_id | unreachable

    def module(self) -> types.ModuleType:
        outer = self
        module = types.ModuleType("resend")

        class Emails:
            @staticmethod
            def send(params):
                outer.api_keys.append(module.api_key)
                outer.sent.append(params)
                if outer.behaviour == "reject":
                    raise ResendError(
                        403,
                        "validation_error",
                        f"You can only send testing emails to your own email address ({OWNER}).",
                    )
                if outer.behaviour == "unreachable":
                    raise ConnectionError("POST body: code=482913")
                if outer.behaviour == "no_id":
                    return {}
                return {"id": "msg_123"}

        module.Emails = Emails
        module.api_key = None
        return module

    def code_in(self, index: int = -1) -> str:
        return re.search(r"\b(\d{6})\b", self.sent[index]["text"]).group(1)


@pytest.fixture
def resend(monkeypatch):
    fake = FakeResend()
    monkeypatch.setitem(sys.modules, "resend", fake.module())
    return fake


@pytest.fixture
def provider():
    return ResendEmailProvider(api_key="re_SECRET_KEY_VALUE", from_address=FROM)


def send_code(provider, to="person@example.com"):
    provider.send_verification_code(
        to_address=to, display_name="P", code="482913", expires_minutes=10
    )


# --- the provider ----------------------------------------------------------------


def test_an_accepted_message_is_one_request_from_the_configured_sender(resend, provider):
    send_code(provider)

    assert len(resend.sent) == 1
    request = resend.sent[0]
    assert request["from"] == FROM
    assert request["to"] == ["person@example.com"]
    assert "482913" in request["text"]  # the code goes to the recipient, nowhere else
    assert resend.api_keys == ["re_SECRET_KEY_VALUE"]


def test_a_rejection_is_a_failure_and_names_the_reason_without_the_owners_address(
    resend, provider, caplog
):
    resend.behaviour = "reject"
    with caplog.at_level(logging.INFO, logger="astrion.email.resend"):
        with pytest.raises(EmailDeliveryError):
            send_code(provider)

    assert "validation_error" in caplog.text and "code=403" in caplog.text
    assert "only send testing emails" in caplog.text
    for never in (OWNER, "person@example.com", "482913", "re_SECRET_KEY_VALUE"):
        assert never not in caplog.text


def test_a_response_without_a_message_id_is_not_a_success(resend, provider, caplog):
    resend.behaviour = "no_id"
    with caplog.at_level(logging.INFO, logger="astrion.email.resend"):
        with pytest.raises(EmailDeliveryError):
            send_code(provider)
    assert "no message id" in caplog.text
    assert "accepted" not in caplog.text


def test_an_unreachable_provider_is_a_failure_reported_by_class_name(resend, provider, caplog):
    resend.behaviour = "unreachable"
    with caplog.at_level(logging.INFO, logger="astrion.email.resend"):
        with pytest.raises(EmailDeliveryError) as raised:
            send_code(provider)
    assert "ConnectionError" in caplog.text
    assert "482913" not in caplog.text and "482913" not in str(raised.value)


def test_success_is_logged_with_the_domain_only(resend, provider, caplog):
    with caplog.at_level(logging.INFO, logger="astrion.email.resend"):
        send_code(provider)
    assert "accepted domain=example.com" in caplog.text
    assert "person@" not in caplog.text and "482913" not in caplog.text


def test_describe_failure_masks_addresses():
    described = describe_failure(ResendError(403, "validation_error", f"only to {OWNER} please"))
    assert OWNER not in described and "<address>" in described


# --- the API, over the real provider ------------------------------------------------


@pytest.fixture
def live(full_db, resend, provider):
    """The application with the real Resend provider over the fake package."""
    app, _ = _client(_settings(full_db, otp_resend_cooldown_seconds=0))
    app.state.email_provider = provider
    with TestClient(app) as client:
        client.resend = resend  # type: ignore[attr-defined]
        yield client


def test_registration_reports_a_send_only_when_the_provider_accepted_it(live):
    body = register(live, "new@example.com").json()

    assert live.resend.sent, "the provider was asked"
    assert body["email_sent"] is True and body["verification"]["email_sent"] is True
    assert live.post("/api/auth/verification/verify", json={"code": live.resend.code_in()}).status_code == 200


def test_a_rejected_registration_send_is_reported_as_not_sent(live):
    live.resend.behaviour = "reject"
    body = register(live, "rejected@example.com").json()

    assert body["email_sent"] is False
    assert body["verification"]["email_sent"] is False
    assert "could not be emailed" in body["message"]


def test_resend_says_code_sent_only_after_acceptance_and_the_old_code_stops(live):
    register(live, "again@example.com")
    first = live.resend.code_in()

    assert live.post("/api/auth/verification/resend").json()["status"] == "code_sent"
    second = live.resend.code_in()
    assert len(live.resend.sent) == 2 and first != second

    assert live.post("/api/auth/verification/verify", json={"code": first}).json()["error"]["code"] == "otp_invalid"
    assert live.post("/api/auth/verification/verify", json={"code": second}).status_code == 200


@pytest.mark.parametrize("behaviour", ["reject", "no_id", "unreachable"])
def test_resend_says_delivery_failed_when_the_provider_did_not_accept(live, behaviour):
    register(live, "fails@example.com")
    live.resend.behaviour = behaviour

    body = live.post("/api/auth/verification/resend").json()

    assert body["status"] == "delivery_failed"
    assert body["verification"]["email_sent"] is False


def test_a_failed_resend_leaves_the_verification_usable(live):
    register(live, "recover@example.com")
    live.resend.behaviour = "reject"
    assert live.post("/api/auth/verification/resend").json()["status"] == "delivery_failed"

    live.resend.behaviour = "accept"
    assert live.post("/api/auth/verification/resend").json()["status"] == "code_sent"
    assert live.post("/api/auth/verification/verify", json={"code": live.resend.code_in()}).status_code == 200


def test_an_expired_code_is_still_refused(full_db, resend, provider):
    app, _ = _client(_settings(full_db, otp_ttl_minutes=1, otp_resend_cooldown_seconds=0))
    app.state.email_provider = provider
    with TestClient(app) as client:
        register(client, "late@example.com")
        code = resend.code_in()
        from app.backend.services.database import get_connection

        conn = get_connection(full_db)
        with conn:
            conn.execute("UPDATE email_otps SET expires_at_utc = '2000-01-01T00:00:00+00:00'")
        conn.close()
        assert client.post("/api/auth/verification/verify", json={"code": code}).json()["error"]["code"] == "otp_expired"


# --- an address that already has an account -------------------------------------------


@pytest.fixture
def taken(full_db, resend, provider):
    app, _ = _client(_settings(full_db, otp_resend_cooldown_seconds=0))
    app.state.email_provider = provider
    from app.backend.services.database import get_connection, initialize_schema

    conn = get_connection(full_db)
    initialize_schema(conn)
    make_user(conn, "taken@example.com")
    conn.close()
    with TestClient(app) as client:
        client.resend = resend  # type: ignore[attr-defined]
        yield client


def test_a_taken_address_gets_a_real_email_that_carries_no_code(taken):
    body = register(taken, "taken@example.com").json()

    assert body["email_sent"] is True  # because the provider really accepted a message
    (request,) = taken.resend.sent
    assert request["to"] == ["taken@example.com"]
    assert "already" in request["subject"].lower()
    assert not re.search(r"\d{6}", request["text"] + request["html"])
    assert "http" not in request["text"]  # nothing to click that would sign anyone in


def test_a_taken_address_whose_notice_is_rejected_reports_not_sent(taken):
    taken.resend.behaviour = "reject"
    body = register(taken, "taken@example.com").json()
    assert body["email_sent"] is False and body["verification"]["email_sent"] is False


def test_resending_on_a_taken_address_sends_another_notice_and_still_verifies_nothing(taken):
    register(taken, "taken@example.com")
    assert taken.post("/api/auth/verification/resend").json()["status"] == "code_sent"
    assert len(taken.resend.sent) == 2
    assert taken.post("/api/auth/verification/verify", json={"code": "000000"}).status_code != 200
    assert taken.get("/api/auth/me").status_code == 401


def test_notices_to_one_address_are_capped_so_registration_cannot_flood_a_mailbox(taken):
    limit = Settings().otp_max_sends_per_window
    results = []
    for _ in range(limit + 2):
        taken.cookies.clear()
        results.append(register(taken, "taken@example.com").json()["email_sent"])

    assert results[:limit] == [True] * limit
    assert results[limit:] == [False, False]
    assert len(taken.resend.sent) == limit  # the provider was not asked past the cap


def test_notices_never_use_up_the_real_owners_allowance_for_codes(full_db, resend, provider):
    """A stranger registering an address over and over must not lock its owner out."""
    app, _ = _client(_settings(full_db, otp_resend_cooldown_seconds=0))
    app.state.email_provider = provider
    from app.backend.services.database import get_connection, initialize_schema

    conn = get_connection(full_db)
    initialize_schema(conn)
    make_user(conn, "owner-unverified@example.com", verified=False)
    conn.close()
    with TestClient(app) as attacker:
        for _ in range(6):
            attacker.cookies.clear()
            register(attacker, "owner-unverified@example.com")
    resend.sent.clear()

    with TestClient(app) as owner:
        response = owner.post(
            "/api/auth/login",
            json={"email": "owner-unverified@example.com", "password": GOOD_PASSWORD},
        )
        assert response.status_code == 401  # routed to verification
        assert response.json()["error"]["details"]["verification"]["email_sent"] is True
    assert len(resend.sent) == 1  # a real code, unaffected by the notices


def test_the_taken_and_free_answers_are_the_same_shape_over_the_real_provider(taken):
    free = register(taken, "free@example.com").json()
    taken.cookies.clear()
    used = register(taken, "taken@example.com").json()
    assert set(free) == set(used)
    assert set(free["verification"]) == set(used["verification"])
    assert free["status"] == used["status"]
    # The wording holds for both, apart from the masked address it names.
    assert re.sub(r"\S+@\S+", "<hint>", free["message"]) == re.sub(r"\S+@\S+", "<hint>", used["message"])
    assert free["email_sent"] is used["email_sent"] is True

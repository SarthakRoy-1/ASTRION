"""Email templates for authentication flows.

Plain text and HTML versions of every email this application sends.
No template engine: string interpolation is enough for two emails, and
removing the dependency removes a class of injection if template input
ever came from user-controlled fields (it does not here, but keeping that
contract obvious is worth more than a template engine's features).

The verification URL is always constructed by the caller from a server-side
token; it is never derived from user input.
"""

from __future__ import annotations


def verification_email_text(*, display_name: str, verification_url: str) -> str:
    name = display_name.replace("\n", " ").replace("\r", "")
    return (
        f"Hi {name},\n\n"
        "Confirm your email address to finish setting up your ASTRION account.\n\n"
        f"Verification link (valid for 24 hours, single use):\n{verification_url}\n\n"
        "If you did not create an ASTRION account, you can ignore this message.\n\n"
        "— The ASTRION team"
    )


def verification_email_html(*, display_name: str, verification_url: str) -> str:
    import html as _html

    safe_name = _html.escape(display_name)
    safe_url = _html.escape(verification_url, quote=True)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Confirm your ASTRION email address</title>
</head>
<body style="font-family:system-ui,sans-serif;background:#f9fafb;margin:0;padding:32px 16px;">
  <table role="presentation" style="max-width:520px;margin:0 auto;background:#fff;border-radius:8px;padding:32px;border:1px solid #e5e7eb;">
    <tr><td>
      <h1 style="font-size:20px;font-weight:600;color:#111827;margin:0 0 16px;">
        Confirm your email address
      </h1>
      <p style="color:#374151;margin:0 0 24px;">
        Hi {safe_name},<br><br>
        Click the button below to verify your ASTRION account.
        The link is valid for 24 hours and can only be used once.
      </p>
      <a href="{safe_url}"
         style="display:inline-block;background:#2563eb;color:#fff;font-weight:600;
                padding:12px 24px;border-radius:6px;text-decoration:none;">
        Verify email address
      </a>
      <p style="color:#6b7280;font-size:13px;margin:24px 0 0;">
        If you did not create an ASTRION account, you can safely ignore this email.
      </p>
      <p style="color:#9ca3af;font-size:12px;margin:8px 0 0;word-break:break-all;">
        Or copy this link: {safe_url}
      </p>
    </td></tr>
  </table>
</body>
</html>"""


# --- one-time verification code ----------------------------------------------

VERIFICATION_CODE_SUBJECT = "Your ASTRION verification code"


def verification_code_email_text(
    *, display_name: str, code: str, expires_minutes: int
) -> str:
    name = display_name.replace("\n", " ").replace("\r", "")
    return (
        f"Hi {name},\n\n"
        "Use this code to verify your email address for ASTRION:\n\n"
        f"    {code}\n\n"
        f"The code expires in {expires_minutes} minutes and can be used once. "
        "Requesting a new code cancels this one.\n\n"
        "Do not share this code with anyone. ASTRION will never ask you for it "
        "by phone, chat or email.\n\n"
        "If you did not try to sign in to or create an ASTRION account, you can "
        "ignore this message; nothing changes without the code.\n\n"
        "— ASTRION"
    )


def verification_code_email_html(
    *, display_name: str, code: str, expires_minutes: int
) -> str:
    import html as _html

    safe_name = _html.escape(display_name)
    # Digits only by construction, escaped anyway so the contract holds even if
    # the code format ever changes.
    safe_code = _html.escape(code)
    minutes = int(expires_minutes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{VERIFICATION_CODE_SUBJECT}</title>
</head>
<body style="margin:0;padding:32px 16px;background:#04090e;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
  <table role="presentation" style="max-width:520px;margin:0 auto;background:#10161d;border:1px solid #26303a;border-radius:16px;padding:36px 32px;color:#f2eee7;">
    <tr><td>
      <p style="margin:0 0 28px;font-size:15px;font-weight:700;letter-spacing:.28em;color:#ebe2d6;">ASTRION</p>
      <h1 style="margin:0 0 12px;font-size:22px;font-weight:600;color:#f2eee7;">Verify your email address</h1>
      <p style="margin:0 0 24px;color:#c9c4bc;line-height:1.55;">
        Hi {safe_name}, enter this code in ASTRION to verify your email address.
      </p>
      <p style="margin:0 0 24px;padding:18px 0;border-radius:12px;background:#ebe2d6;color:#0a0f14;text-align:center;font-size:32px;font-weight:700;letter-spacing:.35em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">
        {safe_code}
      </p>
      <p style="margin:0 0 12px;color:#c9c4bc;line-height:1.55;">
        The code expires in <strong style="color:#f2eee7;">{minutes} minutes</strong>
        and can be used once. Requesting a new code cancels this one.
      </p>
      <p style="margin:0 0 12px;color:#c9c4bc;line-height:1.55;">
        <strong style="color:#f2eee7;">Do not share this code with anyone.</strong>
        ASTRION will never ask you for it by phone, chat or email.
      </p>
      <p style="margin:24px 0 0;color:#8d8a85;font-size:13px;line-height:1.5;">
        If you did not try to sign in to or create an ASTRION account, you can
        ignore this message; nothing changes without the code.
      </p>
    </td></tr>
  </table>
</body>
</html>"""


# --- notice to an address that already has an account -------------------------

EXISTING_ACCOUNT_SUBJECT = "You already have an ASTRION account"


def existing_account_email_text() -> str:
    return (
        "Hello,\n\n"
        "Someone asked to create an ASTRION account with this email address, but "
        "an account for it already exists, so no new one was made.\n\n"
        "If that was you, sign in with the password you chose. If this address "
        "was never verified, signing in will email you a new verification code.\n\n"
        "If it was not you, you can ignore this message; nothing has changed and "
        "nobody has been given access to your account.\n\n"
        "\u2014 ASTRION"
    )


def existing_account_email_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{EXISTING_ACCOUNT_SUBJECT}</title>
</head>
<body style="margin:0;padding:32px 16px;background:#04090e;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
  <table role="presentation" style="max-width:520px;margin:0 auto;background:#10161d;border:1px solid #26303a;border-radius:16px;padding:36px 32px;color:#f2eee7;">
    <tr><td>
      <p style="margin:0 0 28px;font-size:15px;font-weight:700;letter-spacing:.28em;color:#ebe2d6;">ASTRION</p>
      <h1 style="margin:0 0 12px;font-size:22px;font-weight:600;color:#f2eee7;">You already have an account</h1>
      <p style="margin:0 0 16px;color:#c9c4bc;line-height:1.55;">
        Someone asked to create an ASTRION account with this email address, but
        an account for it already exists, so no new one was made.
      </p>
      <p style="margin:0 0 16px;color:#c9c4bc;line-height:1.55;">
        If that was you, sign in with the password you chose. If this address
        was never verified, signing in will email you a new verification code.
      </p>
      <p style="margin:24px 0 0;color:#8d8a85;font-size:13px;line-height:1.5;">
        If it was not you, you can ignore this message; nothing has changed and
        nobody has been given access to your account.
      </p>
    </td></tr>
  </table>
</body>
</html>"""

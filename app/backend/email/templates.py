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

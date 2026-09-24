# Authentication: email + password, Google, GitHub, and email codes

ASTRION has three ways in and one authentication system behind them. Every
method ends in the same place: a row in `sessions`, an `HttpOnly` session
cookie, the same idle and absolute timeouts, the same second-factor rule and
the same `login.succeeded` audit event. The account each method opens is an
ordinary row in `users`.

| Method | Proves | Code |
| --- | --- | --- |
| Email + password | Knowledge of the password (scrypt), plus a verified address | `auth/service.py::login` |
| Continue with Google | Google vouches for the identity and, usually, a verified address | `auth/oauth.py`, `api/identity_routes.py` |
| Continue with GitHub | GitHub vouches for the identity; the address comes from `/user/emails` | same |
| Email code (OTP) | Control of the mailbox | `auth/verification.py`, `api/identity_routes.py` |

A provider sign-in is **one factor**, exactly as a password is. An account with
TOTP enabled gets a half session (`mfa_satisfied = 0`) from any of them and
must answer the existing challenge.

---

## Email verification by one-time code

Registration no longer issues a verification *link*: it sends a six-digit code,
and the verification screen takes it. Links sent before this change still
redeem at `/verify-email` (the endpoint is unchanged), and password reset still
uses links.

### When a code is required

| Situation | What happens |
| --- | --- |
| New email + password registration | Account created unverified; a code is emailed; the code screen opens |
| Verified account signs in with its password | Signs in. No code. |
| Unverified account signs in with the **correct** password | No session. A code is emailed and the code screen opens (`401 email_verification_required`). |
| Unverified account, **wrong** password | The generic "Incorrect email address or password." — nothing reveals the account's state |
| Google sign-in, Google says the address is verified | No code |
| GitHub sign-in with a primary verified address (`/user/emails`) | No code |
| GitHub (or Google) with no verified address | The person enters an address, a code is emailed to it, and only then is an account linked or created |

A correct code verifies the address **and** starts a session, so the flow
continues straight into the existing onboarding (create a workspace) or the
application.

### How it works

A *verification* is a browser's claim to be proving one address. It is held as
an `HttpOnly` cookie, `<SESSION_COOKIE_NAME>_verification`, path `/api/auth`,
whose SHA-256 digest is stored in `email_verifications`. It is issued only to
someone who just created the account or just gave its correct password. For
that reason the verify, resend and address endpoints take **no email address**:
there is nothing to point at somebody else's account.

Codes live in `email_otps`, one row per code sent:

| Control | Implementation | Setting (default) |
| --- | --- | --- |
| Generation | `secrets.randbelow(10**6)`, six digits | — |
| Storage | HMAC-SHA256, keyed by a per-row random salt, over `verification_id:code`. Never plaintext | — |
| Lifetime | Checked at use | `OTP_TTL_MINUTES` (10) |
| Single use | Consumed under a guarded `UPDATE … WHERE consumed_at_utc IS NULL` | — |
| Superseded on resend | A new code retires every earlier live code for the same verification **and** the same address | — |
| Attempt limit | Every submission spends an attempt *before* comparison, under a guarded `UPDATE`; the code is burned at the limit | `OTP_MAX_ATTEMPTS` (5) |
| Resend cooldown | Per verification | `OTP_RESEND_COOLDOWN_SECONDS` (60) |
| Send cap | Per verification **and** per address, over a rolling window, so repeated sign-ins from fresh browsers cannot mint unlimited mail | `OTP_MAX_SENDS_PER_WINDOW` (5) per `OTP_SEND_WINDOW_MINUTES` (60) |
| Verification lifetime | The cookie and its row | `VERIFICATION_TTL_MINUTES` (60) |
| Per-IP limit | `/api/auth/` is in the credential rate-limit bucket | `AUTH_RATE_LIMIT_PER_MINUTE` (10) |

With the defaults an attacker gets at most 25 guesses an hour against one
address, against a space of a million codes.

**No existence oracle.** Registering an address that is already taken returns
exactly what a new registration returns, including a verification: a *decoy*.
The decoy has real code rows, so it expires, counts attempts, enforces
cooldowns and reports sends like a real one, and it can never succeed. Nothing
is emailed to the real owner, and the decoy never counts against their sending
allowance. The one difference a caller could observe is during a live mail
provider outage: the real registration reports "not sent" and the decoy
reports whatever the transport would normally do.

**Never exposed.** The code is not in any API response (in any environment),
redirect, log line or audit entry. The email provider is the only place it
goes. Audit entries record `user.email_code_sent` / `user.email_code_failed`
with the address's domain only.

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /api/auth/register` | Now also returns `verification` (display state) and sets the verification cookie |
| `POST /api/auth/login` | `401 email_verification_required` with `details.verification` for a correct password on an unverified address |
| `GET /api/auth/me` | A `401` now carries `details.verification` when this browser is mid-verification, so a reload returns to the code screen |
| `GET /api/auth/verification` | The verification in progress, if any |
| `POST /api/auth/verification/verify` `{code}` | Check a code. On success: address verified, session started |
| `POST /api/auth/verification/resend` | New code; `429 otp_resend_cooldown` / `otp_send_limit` with `retry_after_seconds` |
| `POST /api/auth/verification/email` `{email}` | Provider sign-ins without a verified address only: choose the address to prove |
| `POST /api/auth/verification/cancel` | Abandon the verification in this browser |

Refusals carry a code the UI branches on: `otp_invalid` (with
`attempts_remaining`), `otp_expired`, `otp_attempts_exceeded`,
`verification_expired`.

### Email delivery

The existing email layer is extended rather than replaced:
`EmailProvider.send_verification_code` is added beside
`send_verification_email`, implemented by the same Resend provider and the same
null provider. The template (`email/templates.py`) carries the ASTRION name,
the purpose, the code, the expiry and a "do not share this code" warning.

| Transport | When | Notes |
| --- | --- | --- |
| Resend | `RESEND_API_KEY` set | Sender from `EMAIL_FROM` |
| Development outbox | `EMAIL_OUTBOX_DIR` set and no Resend key | One `0600` JSON file per message. `python dev.py` sets `data/outbox/` by default. **Refused at startup in production.** |
| None | Neither | Sends fail; the UI shows "The email didn't send" and offers resend |

If delivery fails, the account still exists and the code screen offers a resend
once the cooldown passes.

---

## Sign in with Google or GitHub

OAuth 2.0 authorization code flow with PKCE (S256), run entirely by the
backend. The client secret never reaches the browser, and the provider's
access token is used once, to read the profile, then discarded. It is never
stored or logged.

```text
Browser ── GET /api/auth/oauth/{provider}/start ──► API
                                       sets <cookie>_oauth_state (HttpOnly, SameSite=Lax,
                                       path /api/auth/oauth); stores the state's digest +
                                       PKCE verifier in oauth_states (10-minute TTL)
        ◄── 302 to the provider's consent screen

Browser ── provider ── 302 ──► GET /api/auth/oauth/{provider}/callback?code&state
                                       state must equal the cookie AND name an unused,
                                       unexpired row for this provider → consumed
                                       code + verifier exchanged server-to-server
                                       profile read → account resolved → session cookie
        ◄── 302 to FRONTEND_BASE_URL/sign-in
```

* **CSRF and login CSRF.** The state is bound to the browser that started the
  flow (cookie) and to this server (database row), and it is single use. A
  callback URL replayed in another browser, or twice, is refused.
* **No open redirect.** The callback always returns to
  `FRONTEND_BASE_URL/sign-in`, optionally with one fixed code:
  `?auth_error=oauth_cancelled | oauth_failed | oauth_state | oauth_unavailable
  | oauth_account`, or `?auth=verify_email`. No request parameter chooses the
  destination. The frontend maps each code to its own wording and never
  renders text from the URL.
* **Logs.** uvicorn's access log records request lines with their query
  strings. A filter (`api/app.py::RedactOAuthQuery`) removes the query from
  every `/api/auth/oauth/` line, so authorization codes and states are not
  written there. Failures are logged by status code or exception class only.
* **Cancellation** at the consent screen (`error=access_denied`) returns to
  sign-in with "Sign-in was cancelled."

### Which account a provider identity opens

A user can hold any number of provider identities (`user_identities`, unique on
`(provider, provider_subject)`), alongside or instead of a password.

1. **Already linked.** The identity opens its user, even if the address at the
   provider has since changed.
2. **The provider says the address is verified** (Google `email_verified: true`,
   or GitHub's primary verified address from `/user/emails`, ignoring
   `@users.noreply.github.com`):
   * **An account with that address exists and is verified:** the identity is
     linked to it. Its password keeps working.
   * **An account exists but was never verified:** whoever set its password
     never proved they own the mailbox. This is the "pre-registration takeover",
     where an attacker registers a victim's address and waits. The identity is
     linked and the address marked verified. The unproven password is
     discarded (the owner can set one with a reset link), and any sessions,
     links and codes for the account are revoked.
   * **No account:** a verified account is created with the provider's display
     name and **no password**.
   * **The account is disabled:** refused (`oauth_account`).
3. **No verified address** (GitHub with none, or an unverified one): nothing is
   linked or created from the provider's say-so. The person is asked for an
   address, proves it with an emailed code, and rule 2 runs against the
   address they proved.

**An identity is never linked on the strength of an unverified address.**

Accounts without a password (`UNUSABLE_PASSWORD`) refuse every password
sign-in. `verify_password` still spends a full scrypt derivation, so timing
does not reveal that an account has no password.

### Callback URLs to register

The callback is `{API_PUBLIC_URL}/api/auth/oauth/{provider}/callback`. It must
be registered character for character.

| Environment | Google "Authorized redirect URI" | GitHub "Authorization callback URL" |
| --- | --- | --- |
| Local (`python dev.py`) | `http://localhost:8000/api/auth/oauth/google/callback` | `http://localhost:8000/api/auth/oauth/github/callback` |
| Production | `https://<your-api-host>/api/auth/oauth/google/callback` | `https://<your-api-host>/api/auth/oauth/github/callback` |

`<your-api-host>` is the public origin of the FastAPI service, the same value
the frontend was built with as `NEXT_PUBLIC_API_BASE_URL`, not the Vercel
frontend's origin. A GitHub OAuth App accepts only one callback URL, so create
one app for local development and one for production.

### Console setup

**Google** (Google Cloud Console → APIs & Services):

1. OAuth consent screen: set the app name, support email, and authorised
   domains; scopes `openid`, `email`, `profile`. Publish it, or add test users
   while it is in testing.
2. Credentials → Create credentials → OAuth client ID → *Web application*.
3. Authorised redirect URIs: the callback(s) above. Authorised JavaScript
   origins are not needed: the browser never talks to Google on its own.
4. Put the client ID and secret in `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET` on the **backend** host.

**GitHub** (Settings → Developer settings → OAuth Apps → New OAuth App):

1. Homepage URL: the frontend origin.
2. Authorization callback URL: the GitHub callback above.
3. Generate a client secret. Put both in `GITHUB_OAUTH_CLIENT_ID` /
   `GITHUB_OAUTH_CLIENT_SECRET` on the backend host.
4. Scopes requested: `read:user user:email`. The second is what allows reading
   the verified address.

A provider with no configured credentials is not advertised
(`/health.oauth_providers`). Its button shows as disabled, "… sign-in isn't set
up on this deployment yet", and its start URL returns the browser to sign-in
with `oauth_unavailable`.

---

## Environment variables added

All backend, all optional. Nothing here is ever exposed to the browser;
`/health` reports only the *names* of configured providers.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | unset | Enables "Continue with Google" (both required) |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | unset | Enables "Continue with GitHub" (both required) |
| `API_PUBLIC_URL` | `http://localhost:8000` | Public origin of this API; forms the callback URL. Must be `https://` in production when a provider is configured |
| `FRONTEND_BASE_URL` | `EMAIL_VERIFICATION_URL`, else `http://localhost:3000` | Where the browser returns after a provider sign-in. Must be `https://` in production when a provider is configured |
| `OTP_TTL_MINUTES` | `10` | Code lifetime |
| `OTP_MAX_ATTEMPTS` | `5` | Wrong guesses before a code is burned |
| `OTP_RESEND_COOLDOWN_SECONDS` | `60` | Gap between codes |
| `OTP_MAX_SENDS_PER_WINDOW` / `OTP_SEND_WINDOW_MINUTES` | `5` / `60` | Send cap per verification and per address |
| `VERIFICATION_TTL_MINUTES` | `60` | Lifetime of a verification (its cookie) |
| `EMAIL_OUTBOX_DIR` | unset (`dev.py`: `data/outbox`) | Development mail as files. Refused in production |

Existing, and now also used by codes: `RESEND_API_KEY`, `EMAIL_FROM`.

## Production checklist

The code is complete. **A deployment is not production-ready for provider
sign-in or email codes until the external setup below is done**, and none of it
can be done from this repository:

1. **Email:** a Resend account and API key (`RESEND_API_KEY`), a verified
   sending domain with SPF, DKIM and DMARC records, and `EMAIL_FROM` on that
   domain. Without this, registration cannot complete on the hosted deployment,
   and neither can a GitHub sign-in without a verified address.
2. **Google:** the OAuth client and consent screen above, with the production
   callback registered, and the app published (or users added as testers).
3. **GitHub:** a production OAuth App with the production callback.
4. **Backend host:** `API_PUBLIC_URL` and `FRONTEND_BASE_URL` set to the real
   https origins, the four client credentials added, and `CORS_ALLOW_ORIGINS`
   including the frontend origin.
5. **Cross-site cookies:** the hosted frontend and API are on different sites
   (Vercel and Render), so the session cookie already needs
   `SESSION_COOKIE_SAMESITE=none` with `SESSION_COOKIE_SECURE=true` there. The
   verification cookie follows the same settings. The OAuth state cookie is
   always `SameSite=Lax`, which is correct because the provider returns with a
   top-level navigation. Browsers that block third-party cookies (Safari by
   default, Chrome in some modes) affect every sign-in method equally on a
   split-site deployment. Serving the API under the frontend's site (for
   example `api.<your-domain>`) removes the dependency.

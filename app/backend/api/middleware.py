"""Request-level defences applied before any route runs.

Four middlewares, in the order they must run:

1. `BodySizeLimitMiddleware` — refuse an oversized body before it is buffered.
2. `CsrfOriginMiddleware` — refuse a state-changing request from a foreign origin.
3. `RateLimitMiddleware` — refuse a caller asking too often.
4. `SecurityHeadersMiddleware` — set the response headers a browser enforces.

Order matters and is not arbitrary. Size is checked first because everything
after it costs more; the CSRF check precedes the rate limiter so a cross-origin
attack cannot consume a victim's rate budget; headers are applied on the way
out, to every response including the refusals above.

Starlette applies middleware in reverse registration order, so `create_app`
registers these bottom-up. That inversion is a standing trap, and the comment
at the registration site says so.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.backend.api.ratelimit import RateLimiter, client_identity

logger = logging.getLogger("astrion.security")

#: Methods that cannot change state and therefore need no CSRF defence.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Endpoints whose rate limit is tighter than the default, because each request
#: is either expensive (the agent, which fans out into model calls) or is what
#: an attacker guesses against (the credential endpoints).
_AGENT_PATHS = ("/api/chat",)
#: Credential endpoints. `/api/invitations/` is here because redeeming an
#: invitation is redeeming a credential, and an unauthenticated-adjacent
#: endpoint that consumes a token is exactly what gets guessed against.
_AUTH_PATHS = ("/api/auth/", "/api/invitations/")


def request_path(request: Request) -> str:
    """The path the router actually dispatched on.

    **Never use `request.url.path` for a security decision.** Starlette
    rebuilds `request.url` by concatenating `scheme://{Host header}{path}` and
    re-parsing the result, so a `Host` header containing `/`, `?` or `#` moves
    the path boundary and makes `request.url.path` disagree with the path that
    was actually routed (CVE / PYSEC-2026-161). A request to
    `/api/auth/login` carrying `Host: example.com/health?x=` reconstructs to
    `http://example.com/health?x=/api/auth/login`, whose parsed path is
    `/health` — while the router still dispatches to the login endpoint.

    Every middleware here makes a decision from the path, so every one of them
    would have been bypassable: the rate limiter would have read `/health`,
    taken the exemption, and let an unbounded number of credential guesses
    through. `scope["path"]` is the raw value the router itself uses and is not
    reconstructed from any header.
    """
    return request.scope.get("path", "") or request.url.path


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    """The same envelope `api/errors.py` produces, built without a request.

    Middleware runs outside the exception handlers, so it has to construct the
    contract shape itself. Diverging here would mean a client parsing two
    different error formats depending on how early the refusal happened.
    """
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": extra,
            "request_id": None,
        }
    }
    return JSONResponse(status_code=status, content=body)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Refuse a body larger than the configured ceiling.

    The `message` field's 4000-character cap is a *schema* constraint: Pydantic
    only sees it after the whole body has been read into memory and parsed. An
    unauthenticated caller posting a 2 GB body would exhaust the process long
    before validation ran. This middleware answers 413 from the declared
    Content-Length, and — because a chunked request declares no length — also
    caps what it will read when the header is absent.
    """

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_bytes:
                    return _error(
                        413,
                        "payload_too_large",
                        "The request body exceeds the maximum permitted size.",
                        max_bytes=self._max_bytes,
                    )
            except ValueError:
                return _error(
                    400, "invalid_request", "Content-Length was not a number."
                )
        elif request.method not in SAFE_METHODS:
            # No declared length (chunked transfer). Read it here, bounded, and
            # hand the buffered body forward so the route still sees it.
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > self._max_bytes:
                    return _error(
                        413,
                        "payload_too_large",
                        "The request body exceeds the maximum permitted size.",
                        max_bytes=self._max_bytes,
                    )

            async def receive() -> dict:
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive  # noqa: SLF001 - the supported re-feed

        return await call_next(request)


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """Reject a state-changing request that came from an origin we do not serve.

    With session cookies, the browser attaches credentials to any request a
    page makes — including one made by an attacker's page. `SameSite=Lax` is
    the primary defence and already blocks cross-site POSTs; this is the second
    layer, and the one that still holds if the cookie is ever relaxed to
    `SameSite=None` for a cross-origin deployment.

    A request with **no** `Origin` header is allowed through. Browsers always
    send `Origin` on cross-origin state-changing requests, so its absence means
    a non-browser client — curl, a server-to-server call, the test suite — for
    which CSRF is not a meaningful threat. Refusing those would break every
    legitimate API client to defend against an attack they cannot carry out.
    """

    def __init__(self, app, *, allowed_origins: tuple[str, ...]) -> None:
        super().__init__(app)
        self._allowed = {self._normalise(o) for o in allowed_origins}

    @staticmethod
    def _normalise(origin: str) -> str:
        parsed = urlparse(origin.strip())
        if not parsed.scheme or not parsed.netloc:
            return origin.strip().rstrip("/").lower()
        return f"{parsed.scheme}://{parsed.netloc}".lower()

    async def dispatch(self, request: Request, call_next):
        if request.method in SAFE_METHODS:
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin is None:
            return await call_next(request)

        if self._normalise(origin) not in self._allowed:
            logger.warning(
                "cross-origin %s %s refused from %s",
                request.method,
                request_path(request),
                origin,
            )
            return _error(
                403,
                "cross_origin_refused",
                "This request came from an origin this deployment does not serve.",
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Cap how often one caller may ask, with a tighter cap where it matters.

    Keyed on the authenticated user when there is one and the client address
    otherwise, so a single account cannot escape its limit by reconnecting and
    a single address cannot escape by registering more accounts.
    """

    def __init__(
        self,
        app,
        *,
        limiter: RateLimiter,
        default_limit: int,
        agent_limit: int,
        auth_limit: int,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._default = default_limit
        self._agent = agent_limit
        self._auth = auth_limit

    def _bucket_for(self, path: str) -> tuple[str, int]:
        if any(path.startswith(p) for p in _AUTH_PATHS):
            return "auth", self._auth
        if any(path.startswith(p) for p in _AGENT_PATHS):
            return "agent", self._agent
        return "default", self._default

    async def dispatch(self, request: Request, call_next):
        # Health is exempt: it is what a load balancer and the frontend's
        # cold-start probe call, and rate-limiting it would make an unhealthy
        # deployment look dead.
        if request_path(request) == "/health":
            return await call_next(request)

        bucket, limit = self._bucket_for(request_path(request))
        allowed, retry_after = self._limiter.check(
            bucket, client_identity(request), limit
        )
        if not allowed:
            response = _error(
                429,
                "rate_limited",
                "Too many requests. Slow down and try again shortly.",
                retry_after_seconds=retry_after,
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set the response headers a browser will enforce on our behalf.

    The API serves JSON, not HTML, so the policy here is the strictest one
    that can possibly be correct: a CSP that permits nothing at all, and frame
    denial. Neither costs the API anything, and both matter because a JSON
    response rendered directly in a browser tab — which happens whenever
    someone opens an endpoint by hand — is otherwise a live document.

    The *interesting* CSP is the frontend's, in `app/frontend/next.config.ts`,
    because that is the response that actually loads scripts.
    """

    def __init__(
        self, app, *, hsts_enabled: bool, hsts_max_age: int
    ) -> None:
        super().__init__(app)
        self._hsts_enabled = hsts_enabled
        self._hsts_max_age = hsts_max_age

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        # Nothing this API returns should ever be executed, embedded or framed.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'none'"
        )
        # Stops a browser from second-guessing a declared Content-Type, which is
        # how a JSON response gets treated as HTML and its contents executed.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Never leak a path or query string to a third-party origin.
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), camera=(), microphone=(), payment=(), usb=(), "
            "interest-cohort=()"
        )
        # Answers to authenticated requests are per-user by construction.
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"

        # Sent only when the deployment is actually served over TLS. Announcing
        # HSTS from a plaintext origin pins a scheme the site cannot honour and
        # can make it unreachable.
        if self._hsts_enabled:
            response.headers["Strict-Transport-Security"] = (
                f"max-age={self._hsts_max_age}; includeSubDomains; preload"
            )
        return response

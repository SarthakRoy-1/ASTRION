# Submission Checklist

The ParcelPilot assessment asks for six deliverables:

1. a repository;
2. a hosted application;
3. a demo video of about five minutes;
4. an architecture note;
5. a product note;
6. a link to try the agent.

This list records what has been **verified** and what still needs a **human
action outside the repository**.

**Status as of 2026-09-16.** Production runs commit `418a5d2` (*fix: close
final portfolio blockers*). This checklist and the other submission documents
are docs-only changes on top of it.

| Item | Link |
| --- | --- |
| Repository | <https://github.com/SarthakRoy-1/ASTRION> |
| Hosted application / agent link | <https://astrion-app.vercel.app/> |
| API | <https://parcelpilot-api-7ro7.onrender.com> (`/health`) |

## Checklist

- [x] **Public GitHub repository**: **verified.**
  - `github.com/SarthakRoy-1/ASTRION` returns 200 without authentication.
  - `main` contains `418a5d2`.
- [x] **Hosted application**: **verified.**
  - `https://astrion-app.vercel.app/` returns 200.
  - The old `parcelpilot-taupe.vercel.app` URL redirects (307) to it.
  - The backend `/health` reports `app_env: production`, `auth_mode: session`,
    `database_ready: true`, `documents_indexed: 6`, `demo_login_enabled: true`.
- [ ] **Demo video**: **human action required.**
  - Record about 5 minutes following [demo-script.md](demo-script.md).
  - Upload it (for example unlisted on YouTube, or Loom).
  - Add the link to the submission form, and optionally to the README.
- [x] **Architecture note**: **verified.**
  - [architecture.md](architecture.md) §0 gives the overview, the principle,
    the diagram, the components and the source-authority tiers.
  - §1–18 are the detailed record.
- [x] **Product note**: **verified.**
  - [product.md](product.md) covers the problem, the product, user contexts,
    proactive detection, trust, confirmation-gated actions, future work, what
    was left out, and one success metric.
- [x] **Agent link**: **verified.**
  - <https://astrion-app.vercel.app/> → **Sign in to the demo** signs in with
    one click as the demo operations member. No password is needed.
  - Since the public landing page was added, that button is hidden from the
    public UI (`PUBLIC_DEMO_SIGN_IN_ENABLED` in
    `app/frontend/src/lib/features.ts`); the backend endpoint and demo tenant
    are unchanged.
- [x] **README**: **verified.**
  - Shows the live URL as the primary link.
  - No stale ParcelPilot URL is presented as primary.
  - No credentials are shown.
  - All relative links resolve: architecture, product note, demo script,
    security, reference, future plan, this checklist.
- [x] **Screenshots**: **verified.**
  - The nine images in `docs/screenshots/` were captured from a real browser
    against the build of the current product in `418a5d2`: sign-in,
    precedence, investigation, pending action, confirmed action, documents,
    operations, audit trail, mobile.
  - They are not mockups. There have been no UI changes since.
- [x] **Final production smoke test**: **verified** against the live
  deployment on 2026-09-16:
  - Demo login returns 200.
  - *"Can Northstar cancel ORD-1001…"* returns fee waived, trust `confident`,
    and *Northstar agreement §2 (tier 1) outranks SOP §1 (tier 3)*.
  - *"Is ORD-2002 eligible…"* returns INR 300.00 from LumenWorks §3.
  - *"What is the weather in Mumbai today?"* returns `insufficient_data`.
  - *"Has TKT-501 breached…"* returns `conditional`, with no breach asserted
    because severity is not set.
  - Operations returns six ranked signals.
  - Documents lists six documents with the correct status and tier.
  - The confirm → execute → replay 409 → audit flow was verified live after
    `418a5d2` deployed.
- [x] **Repository contains no secrets**: **verified.**
  - A `git grep` of tracked files for API-key, token and private-key patterns
    found nothing.
  - No `.env`, `.pem` or `.key` files are tracked.
  - `data/uploads/` is git-ignored.
  - The backend has a default demo password (`DEFAULT_DEMO_PASSWORD`). It is
    a documented, published credential for the synthetic demo workspace, not a
    production secret, and can be overridden with `DEMO_PASSWORD`.
- [x] **Demo credentials not exposed in frontend**: **verified.**
  - The demo password appears in no file under `app/frontend/`.
  - It is in none of the live JavaScript bundles served by Vercel.
  - It is not in the `POST /api/auth/demo-login` response headers or body.
  - `NEXT_PUBLIC_DEMO_PASSWORD` no longer exists.
- [x] **No uncommitted required code changes**: **verified.**
  - Everything that runs in production is on `origin/main` (`418a5d2`).
  - The local checkout at `C:\Projects\ParcelPilot` is still on
    `integration/astrion-document-system`. Its uncommitted files are *older*
    copies of work that has since been committed, for example the pre-`418a5d2`
    `DEFAULT_DEMO_EMAIL`.
  - It also contains the untracked raw brand files in `assets/`, which the app
    does not use.
  - None of it is required. Do not deploy from that checkout.

## Test and build status at `418a5d2`

| Check | Result |
| --- | --- |
| Backend (`pytest`) | 1160 passed |
| Frontend (`vitest`) | 247 passed |
| Typecheck | clean |
| Production build | clean |

## Remaining human actions

1. **Record** the demo video from [demo-script.md](demo-script.md). Wake the
   backend a minute or two beforehand.
2. **Upload** it and copy the link.
3. **Submit** the form with:
   - the repository URL;
   - the hosted URL (which is also the agent link);
   - the video link;
   - links to `docs/architecture.md` and `docs/product.md` on GitHub.
4. *(Optional)* Add the video link to the README's **Live demo** section.

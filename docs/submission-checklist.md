# Submission Checklist

The ParcelPilot assessment asks for six deliverables:

1. a repository;
2. a hosted application;
3. a demo video of about five minutes;
4. an architecture note;
5. a product note;
6. a link to try the agent.

This list records what has been **verified**, how, and what still needs a
**human action outside the repository**. It says "not verified" where that is the
truth.

**Status as of 2026-09-26.** The repository's `main` is `77d3e7a` (PostgreSQL
persistence and object storage). Render and Vercel deploy from `main`. The
ticket-investigation, authentication-diagnostics and interface changes described
here are on the branch `fix/production-bugs-and-polish`, **not yet merged or
deployed**, so the hosted application does not yet behave as the notes describe
in §20 of the architecture note. The checks marked *local* were run against a
throwaway server from this branch.

| Item | Link |
| --- | --- |
| Repository | <https://github.com/SarthakRoy-1/ASTRION> |
| Hosted application | <https://astrion-app.vercel.app/> |
| API | <https://parcelpilot-api-7ro7.onrender.com> (`/health`) |

## Checklist

- [x] **Public GitHub repository**: **verified 2026-09-26.**
  - `github.com/SarthakRoy-1/ASTRION` returns 200 without authentication.
  - `main` is `77d3e7a`.
- [x] **Hosted application is up**: **verified 2026-09-26.**
  - `https://astrion-app.vercel.app/` returns 200, and the old
    `parcelpilot-taupe.vercel.app` URL redirects (307).
  - `/health` reports `status: ok`, `app_env: production`, `auth_mode: session`,
    `provider_mode: deterministic`, `demo_login_enabled: false`,
    `database_ready: true`, `documents_indexed: 4`, and
    `oauth_providers: [google, github]`.
- [ ] **Demo video**: **human action required.**
  - Record about 5 minutes following [demo-script.md](demo-script.md), on a
    workspace that has had the snapshot imported (below).
  - Upload it (for example unlisted on YouTube, or Loom) and add the link to the
    submission form, and optionally to the README.
- [x] **Architecture note**: [architecture.md](architecture.md).
  - §0 is the overview; §1–18 are the design record, with notes where production
    has moved on; §19 is production as it runs now; §20 is how a ticket
    investigation reaches its answer.
- [x] **Product note**: [product.md](product.md).
  - Problem, product, user contexts, proactive detection, trust, confirmation-gated
    actions, future work, what was left out, and one success metric.
- [ ] **A way for a reviewer to try the agent**: **needs a human decision and
      action.** The path is the ordinary one, and it is not open by itself:
  1. **Sign-in.** There is no shared demo login on the hosted deployment
     (PostgreSQL refuses it). A reviewer registers, enters an emailed code, and
     creates a workspace, or continues with Google or GitHub.
     - **Verified locally** end to end with real session authentication and a
       throwaway database: register, wrong code refused, right code verifies,
       replay refused, sign out, sign in with the password, create a workspace,
       ask, prepare, confirm, audit (32 checks).
     - **Not verified on the hosted deployment**: email delivery (on Resend's
       testing sender only the account owner's address receives mail), and Google
       and GitHub sign-in (GitHub was failing when last tested and has not been
       root-caused). Check the Resend dashboard for the send and the Render log for
       `resend`, `oauth github` lines after trying once with a real mailbox.
  2. **Data.** A new workspace has only the four system documents and no
     customer records, so every question about `ORD-1001` is answered *not found*.
     Someone with the production `DATABASE_URL` must import the snapshot into the
     workspace the reviewer will use:

     ```powershell
     python scripts/ingest_dataset.py   --org-id <workspace-id>
     python scripts/ingest_documents.py --org-id <workspace-id>
     ```

     There is no in-app way to do this yet. **Not done on production.**
  3. **Access to that workspace.** The reviewer has to join it (workspace code and
     password, or an invitation) or the snapshot has to be imported into the
     workspace they created.
- [x] **README**: current with `main` and this branch.
  - No credentials are shown; the demo login is described as local only.
  - The screenshots were captured at `418a5d2`, before the visual refresh, and the
    README says so.
- [x] **Repository contains no secrets**: **verified 2026-09-26.**
  - A scan of tracked files for API-key, token, connection-string and private-key
    patterns found only a placeholder connection string in a test file.
  - No `.env`, `.pem` or `.key` files are tracked.
  - `DEFAULT_DEMO_PASSWORD` exists for the local, synthetic demo and is refused
    against PostgreSQL; production has no demo login.
- [x] **Confirmation gate**: **verified locally and by tests.**
  - `/api/chat` prepares and never executes; a confirmation without the reviewed
    proposal's fingerprint is refused (422); a tampered fingerprint, another
    conversation, a foreign account, a read-only role and a replay are all
    refused; the correct fingerprint executes exactly once.
- [ ] **Production smoke test of the agent**: **not run on this branch's behaviour,
      because it is not deployed.** After a merge and deploy, with the snapshot
      imported, check:
  - `Can Northstar cancel ORD-1001 …` returns fee waived **and** *cannot be
    confirmed yet* naming `TKT-504` and KI-211;
  - `Can LumenWorks cancel ORD-2001 …` returns INR 250;
  - `Is ORD-2002 eligible …` returns INR 300 from the LumenWorks agreement;
  - `Investigate TKT-501 …` reads the response clock, indicates the P1 definition
    without setting a severity, and advises escalation;
  - `What is the weather in Mumbai today?` returns *Not enough information* and no
    escalation advice;
  - a confirmation without a fingerprint returns 422.

## Test and build status

Run on this branch before the documentation was finalised:

| Check | Result |
| --- | --- |
| Backend (`pytest`, SQLite) | 1493 passed, 47 skipped, 0 failed |
| Frontend (`vitest`) | 361 passed (23 files) |
| Typecheck | `tsc --noEmit` clean |
| Production build | `next build` clean |

The backend suite was not run against PostgreSQL for this change
(`ASTRION_TEST_DATABASE_URL` unset); the last PostgreSQL run is the one recorded
with the persistence work.

## Remaining human actions

1. **Decide the merge.** The branch is pushed and validated locally; merging to
   `main` deploys it.
2. **Import the snapshot** into the reviewer workspace on production (above).
3. **Try the sign-in path with a real mailbox** (and GitHub, if you want it
   offered), and read the Resend and Render logs if it does not arrive.
4. **Record** the demo video from [demo-script.md](demo-script.md), and upload it.
5. **Submit** the form with the repository URL, the hosted URL, the video link, and
   links to `docs/architecture.md` and `docs/product.md` on GitHub.

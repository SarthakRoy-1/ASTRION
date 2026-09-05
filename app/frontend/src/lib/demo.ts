/**
 * The published demo account, when a deployment has chosen to publish one.
 *
 * A hosted deployment running real authentication has no way in for a visitor:
 * registration issues a verification link, there is no mail transport to
 * deliver it, and an unverified account cannot sign in. The answer is not to
 * weaken verification — it is to seed ordinary accounts and publish one, which
 * `scripts/seed_demo.py` does.
 *
 * **These values are public by construction.** Next.js inlines `NEXT_PUBLIC_*`
 * into the client bundle at build time, so anything returned here is readable
 * by anyone who opens the page. That is the point: a public demo means anyone
 * may sign in. What keeps it safe is everything *around* it — the account is
 * an ordinary member of one workspace holding synthetic data, and every
 * server-side control (RBAC, tenant scoping, the confirmation gate, manager
 * approval, audit authorization) applies to it exactly as to any other.
 *
 * They are deployment configuration, never repository content: unset in this
 * repo, unset in `.env.example`, and supplied by the hosting platform. With
 * them unset this returns `null` and the sign-in page shows no demo panel at
 * all — a self-hosted copy is not silently advertising credentials it does not
 * have.
 *
 * Read statically rather than through a computed key, because that is what
 * makes Next.js inline them.
 */
export interface DemoAccess {
  email: string;
  /** Absent when the deployment published an address but not a password. */
  password: string | null;
}

export function demoAccess(): DemoAccess | null {
  const email = process.env.NEXT_PUBLIC_DEMO_EMAIL?.trim();
  if (!email) return null;
  const password = process.env.NEXT_PUBLIC_DEMO_PASSWORD?.trim();
  return { email, password: password || null };
}

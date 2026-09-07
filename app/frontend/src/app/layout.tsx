import type { Metadata, Viewport } from "next";

import { AppFrame } from "./AppFrame";
import { AppProviders } from "./providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "ASTRION — AI Logistics Support",
  description:
    "ASTRION is an AI-powered logistics support and operations platform. Ask. Track. Solve. Ship.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

/**
 * Rendered per request, so the CSP nonce can be a per-request value.
 *
 * `src/middleware.ts` issues a fresh nonce for every document and the policy
 * admits inline scripts carrying it. A prerendered page has its HTML fixed at
 * build time, so its script tags could only ever carry a build-time nonce —
 * which would never match, and which is why the production deployment was
 * serving markup that never hydrated.
 *
 * This costs nothing here. Every page below is a client component that fetches
 * its own data at runtime; there has never been server-rendered content worth
 * caching, only an empty shell.
 */
export const dynamic = "force-dynamic";

/**
 * The session and the transcript live here rather than in a page.
 *
 * A Next.js layout persists across child routes, so hoisting them means moving
 * between Support, Operations and Workspace changes only the page body: no
 * request is re-issued, the workspace context is unbroken, and an operations
 * investigation handed to the assistant survives the navigation that started
 * it.
 */
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <AppProviders>
          <AppFrame>{children}</AppFrame>
        </AppProviders>
      </body>
    </html>
  );
}

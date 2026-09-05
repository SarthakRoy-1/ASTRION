import type { Metadata, Viewport } from "next";

import { AppFrame } from "./AppFrame";
import { AppProviders } from "./providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "ParcelPilot Support Agent",
  description:
    "Evidence-backed support and operations assistant for ParcelPilot staff and customers.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

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

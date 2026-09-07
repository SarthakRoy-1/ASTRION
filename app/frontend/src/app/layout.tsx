import type { Metadata, Viewport } from "next";

import { AppFrame } from "./AppFrame";
import { AppProviders } from "./providers";
import "./globals.css";

export const metadata: Metadata = {
  title: "ASTRION — AI Logistics Support",
  description: "ASTRION is an AI-powered logistics support and operations platform.",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg", apple: "/favicon.svg" },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#17212B" };
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <AppProviders><AppFrame>{children}</AppFrame></AppProviders>
      </body>
    </html>
  );
}

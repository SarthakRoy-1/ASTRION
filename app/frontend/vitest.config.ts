import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      // The App Router's navigation hooks and `<Link>` need a router context
      // that does not exist under jsdom. Aliasing them to small working
      // doubles lets every test mount the real application shell — navigation,
      // active state, deep-linked selection and all — rather than a stripped
      // page that would not exercise any of it.
      "next/navigation": fileURLToPath(
        new URL("./src/test/next-navigation.ts", import.meta.url),
      ),
      "next/link": fileURLToPath(
        new URL("./src/test/next-link.tsx", import.meta.url),
      ),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});

import type { NextConfig } from "next";

/**
 * The UI is a pure client of the FastAPI backend: it holds no database
 * connection, no secrets, and no business rules. The only configuration it
 * needs is where that backend lives.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  // Next.js otherwise writes its own agent instruction files into this directory.
  // The repository documents itself in README.md and docs/architecture.md.
  agentRules: false,
};

export default nextConfig;

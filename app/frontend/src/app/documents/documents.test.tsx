import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DocumentsPage from "./page";
import health from "@/test/fixtures/health.json";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

/**
 * The documents area under a real session.
 *
 * What it owes the reader is narrow: the documents their workspace can read,
 * with each one's status stated, and the management controls only when their
 * role actually grants them. The page decides nothing — the server scopes the
 * list and re-checks every upload and delete — but a page that looked for a
 * permission under the wrong name would hide the feature from everyone, which
 * is exactly how it shipped before this test existed.
 */

const DOCUMENTS = [
  {
    document_id: "01_support_policy_v3_current",
    source_file: "01_Support_Policy_v3_CURRENT.pdf",
    title: "ParcelPilot Support Policy v3",
    document_type: "support_policy",
    status: "CURRENT",
    status_raw: "CURRENT",
    is_current: true,
    is_deprecated: false,
    is_authoritative: true,
    authority_tier: 2,
    account_id: null,
    customer_name: null,
    plan: null,
    effective_date_raw: null,
    effective_date: null,
    updated_date_raw: null,
    updated_date: null,
    term_raw: null,
    term_start: null,
    term_end: null,
    supersedes: null,
    superseded_by: null,
    page_count: 2,
  },
  {
    document_id: "02_support_policy_v2_deprecated",
    source_file: "02_Support_Policy_v2_DEPRECATED.pdf",
    title: "ParcelPilot Support Policy v2",
    document_type: "support_policy",
    status: "DEPRECATED",
    status_raw: "DEPRECATED - DO NOT USE FOR CURRENT REQUESTS",
    is_current: false,
    is_deprecated: true,
    is_authoritative: false,
    authority_tier: 4,
    account_id: null,
    customer_name: null,
    plan: null,
    effective_date_raw: null,
    effective_date: null,
    updated_date_raw: null,
    updated_date: null,
    term_raw: null,
    term_start: null,
    term_end: null,
    supersedes: null,
    superseded_by: null,
    page_count: 2,
  },
];

function stubSession(permissions: string[]) {
  const urls: string[] = [];
  const json = (status: number, body: unknown) =>
    Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      statusText: "",
      json: async () => body,
    } as Response);

  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      urls.push(url);

      if (url.includes("/health")) return json(200, { ...health, auth_mode: "session" });
      if (url.includes("/api/principals")) return json(200, { principals: [] });
      if (url.includes("/api/auth/me")) {
        return json(200, {
          user_id: "USR-1",
          display_name: "Ada Owner",
          org_id: "ORG-1",
          org_name: "Northstar Logistics",
          role: "owner",
          permissions,
          account_scope: ["ACCT-001"],
          memberships: [],
          auth_mode: "session",
        });
      }
      if (url.includes("/api/workspaces")) {
        return json(200, {
          workspaces: [
            {
              workspace_id: "ORG-1",
              name: "Northstar Logistics",
              slug: "northstar-logistics",
              created_at_utc: "2026-08-01T00:00:00Z",
              role: "owner",
              permissions,
            },
          ],
          active_workspace_id: "ORG-1",
          needs_workspace: false,
        });
      }
      if (url.endsWith("/api/documents")) return json(200, { documents: DOCUMENTS });
      throw new Error(`unexpected request: ${url}`);
    }),
  );
  return { urls };
}

async function renderDocuments(permissions: string[]) {
  setTestRoute("/documents");
  const stub = stubSession(permissions);
  renderApp(<DocumentsPage />);
  await screen.findByText("ParcelPilot Support Policy v3");
  return stub;
}

describe("the documents area", () => {
  it("offers upload to a role that holds the backend's manage_documents permission", async () => {
    await renderDocuments(["read_documents", "manage_documents"]);

    expect(await screen.findByRole("button", { name: /upload document/i })).toBeInTheDocument();
  });

  it("offers no upload to a role without it", async () => {
    await renderDocuments(["read_documents"]);

    expect(screen.queryByRole("button", { name: /upload document/i })).toBeNull();
  });

  it("states each document's status, so a deprecated policy never reads as current", async () => {
    await renderDocuments(["read_documents"]);

    expect(screen.getByText("CURRENT")).toBeInTheDocument();
    expect(screen.getByText("DEPRECATED")).toBeInTheDocument();
  });
});

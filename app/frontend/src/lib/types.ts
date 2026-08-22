/**
 * The API contract, named.
 *
 * Every type here is an alias over `api-schema.d.ts`, which is *generated*
 * from the backend's own OpenAPI document (`python scripts/export_openapi.py`
 * then `npm run generate:api`). Nothing in the frontend re-declares a backend
 * field by hand, so a rename in `app/backend/api/schemas.py` surfaces as a
 * TypeScript error here rather than as `undefined` in the browser.
 */

import type { components } from "./api-schema";

type Schemas = components["schemas"];

export type ChatRequest = Schemas["ChatRequest"];
export type ChatResponse = Schemas["ChatResponse"];
export type SourceRef = Schemas["SourceRef"];
export type ToolUse = Schemas["ToolUse"];
export type PolicyDecisionView = Schemas["PolicyDecisionView"];
export type ProposedActionView = Schemas["ProposedActionView"];
export type ExecutedActionView = Schemas["ExecutedActionView"];
export type ActionConfirmationRequest = Schemas["ActionConfirmationRequest"];
export type ActionConfirmationResponse = Schemas["ActionConfirmationResponse"];
export type PrincipalView = Schemas["PrincipalView"];
export type PrincipalsResponse = Schemas["PrincipalsResponse"];
export type HealthResponse = Schemas["HealthResponse"];

export type ActionState = Schemas["ActionState"];
export type ResponseOutcome = Schemas["ResponseOutcome"];
export type ConfirmationDecision = Schemas["ConfirmationDecision"];
export type Role = Schemas["Role"];

/**
 * The error envelope every backend failure path produces. It is not in the
 * generated schema because no route *declares* it — it is produced by the
 * application's exception handlers (`app/backend/api/errors.py`), which sit
 * outside the route signatures FastAPI documents.
 */
export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  request_id?: string | null;
}

export interface ApiErrorEnvelope {
  error: ApiErrorBody;
}

/** Roles that the backend permits to prepare and confirm state changes. */
const STATE_CHANGING_ROLES: ReadonlySet<string> = new Set([
  "support_agent",
  "support_manager",
]);

/**
 * Whether this role may confirm an action.
 *
 * Presentation only — it decides whether a confirmation control is worth
 * showing, never whether the action is permitted. The backend re-checks the
 * role on every confirmation and refuses independently of what the UI drew.
 */
export function mayChangeState(role: Role | string): boolean {
  return STATE_CHANGING_ROLES.has(role);
}

/** An action state that is finished: nothing further will happen to it. */
export function isTerminalAction(state: ActionState): boolean {
  return state !== "none" && state !== "pending_confirmation";
}

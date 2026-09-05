/**
 * The five trust states, written for a person.
 *
 * Phase 2 made trust a real, structured field the backend derives in code from
 * tool results — there is no score, no model self-assessment, and nothing here
 * is inferred in the browser. This module only supplies the words and the tone
 * each state wears, in one place, so the chip on an answer's byline and the
 * block beneath it can never describe the same status differently.
 *
 * Colour is never the carrier. Every state has a distinct name, a distinct
 * one-line meaning and a distinct glyph, so `conditional` and
 * `insufficient_data` — which sit in the same warm half of a three-colour
 * palette — stay tellable apart in greyscale and to a screen reader.
 */

import type { PillTone } from "@/components/StatusPill";
import type { ChatResponse } from "./types";

export type TrustStatus =
  | "confident"
  | "conditional"
  | "conflict"
  | "insufficient_data"
  | "escalate";

export interface TrustDescriptor {
  /** The state's name, as it appears on screen. */
  label: string;
  /** What the state means, in one sentence. */
  lede: string;
  /** What the reader should do about it. */
  next: string;
  tone: PillTone;
  glyph: string;
}

const DESCRIPTORS: Record<TrustStatus, TrustDescriptor> = {
  confident: {
    label: "Confident",
    lede: "Every input this answer needed was available and consistent.",
    next: "Act on it.",
    tone: "ok",
    glyph: "✓",
  },
  conditional: {
    label: "Conditional",
    lede: "This holds only if the points below are true. Check them before acting.",
    next: "Confirm the conditions, then act.",
    tone: "caution",
    glyph: "!",
  },
  conflict: {
    label: "Sources conflict",
    lede: "Sources of equal authority disagree. This has not been resolved.",
    next: "Do not act on either source until the conflict is settled.",
    tone: "fail",
    glyph: "≠",
  },
  insufficient_data: {
    label: "Not enough information",
    lede: "Some of what this answer needed was missing or unavailable.",
    next: "Supply what is missing, or escalate.",
    tone: "neutral",
    glyph: "?",
  },
  escalate: {
    label: "Needs a person",
    lede: "This cannot be settled automatically and should be escalated.",
    next: "Hand this to someone who can decide.",
    tone: "fail",
    glyph: "→",
  },
};

/**
 * An unrecognised status is reported as itself rather than mapped onto a
 * neighbour. A trust state this UI has not been taught about is exactly the
 * case where guessing would be worst: rendering it as "Confident" because it
 * is not on the list would be the single most damaging default available.
 */
export function trustDescriptor(status: string): TrustDescriptor {
  return (
    DESCRIPTORS[status as TrustStatus] ?? {
      label: status.replace(/_/g, " "),
      lede: "This answer carries a reliability status this interface does not recognise.",
      next: "Treat it as unsettled.",
      tone: "neutral" as PillTone,
      glyph: "?",
    }
  );
}

/** Tier 1 is a signed customer agreement — see the backend authority model. */
export const CUSTOMER_AGREEMENT_TIER = 1;

type Trust = ChatResponse["trust"];

/** Whether a signed customer agreement decided this answer. */
export function agreementGoverned(trust: Trust): boolean {
  if (!trust) return false;
  return (
    trust.customer_agreement_applied ||
    trust.governing_authority_tier === CUSTOMER_AGREEMENT_TIER
  );
}

/**
 * What decided the answer, named by its authority tier.
 *
 * The same tier vocabulary the evidence section uses, phrased as a statement
 * about the answer rather than about a document — so "Decided by the current
 * support policy" and a Sources list headed "Current support policy" are
 * visibly the same claim.
 */
const GOVERNED_BY: Record<number, string> = {
  1: "Decided by this account's signed agreement",
  2: "Decided by the current support policy",
  3: "Decided by current operational documentation",
  4: "No authoritative source governed this",
};

export function governedByLabel(trust: Trust): string | null {
  const tier = trust?.governing_authority_tier;
  if (tier === null || tier === undefined) return null;
  return GOVERNED_BY[tier] ?? `Decided by tier ${tier} material`;
}

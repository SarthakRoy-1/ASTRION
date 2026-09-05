"use client";

import { createContext, useContext } from "react";

import { useConversation, type Conversation } from "@/hooks/useConversation";
import { useWorkspaceSession, type WorkspaceSession } from "@/hooks/useWorkspaceSession";

/**
 * The two pieces of state every screen shares, hoisted above the router.
 *
 * Phase 4 turned one page into three areas. Both of these had to move out of
 * the page for the same reason:
 *
 * - **The session must be resolved once.** It is what decides whether anything
 *   renders at all, and probing `/health` per route would make a sleeping
 *   backend answer the same cold-start question three times.
 * - **The transcript must survive navigation.** Going from a signal to the
 *   assistant and back is the core operations workflow; a conversation that
 *   reset on every route change would make it unusable.
 *
 * Because a Next.js layout persists across child routes, holding these here
 * means moving between Support, Operations and Workspace re-renders the page
 * body and nothing else. No request is re-issued and no transcript is lost.
 */

const ConversationContext = createContext<Conversation | null>(null);
const SessionContext = createContext<WorkspaceSession | null>(null);

export function AppProviders({ children }: { children: React.ReactNode }) {
  const conversation = useConversation();
  // Resolved from the health the conversation already fetched, so a sleeping
  // backend is probed once rather than twice.
  const session = useWorkspaceSession(conversation.health);

  return (
    <ConversationContext.Provider value={conversation}>
      <SessionContext.Provider value={session}>{children}</SessionContext.Provider>
    </ConversationContext.Provider>
  );
}

/**
 * The two accessors throw rather than returning null.
 *
 * A component that reads either of these outside the provider is a mounting
 * mistake, and the alternative — handing back an empty session — would render
 * a signed-out shell inside a signed-in app and look like a logout bug.
 */
export function useSession(): WorkspaceSession {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession used outside AppProviders");
  return session;
}

export function useChat(): Conversation {
  const conversation = useContext(ConversationContext);
  if (!conversation) throw new Error("useChat used outside AppProviders");
  return conversation;
}

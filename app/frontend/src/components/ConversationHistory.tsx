"use client";

import { useId } from "react";

import type { ConversationSummary } from "@/hooks/useConversation";

import styles from "./ConversationHistory.module.css";

/**
 * Earlier conversations for the identity currently in context.
 *
 * The list it receives is already scoped by the state machine to one identity,
 * so there is nothing to filter here — a component that never sees another
 * customer's threads cannot leak one. The label states whose conversations
 * these are, because "History" alone would leave a demo audience guessing
 * whether the list spans contexts.
 *
 * `<details>` rather than a scripted popover: keyboard-operable, announced
 * correctly, and closed by default so it never competes with the transcript.
 */
export function ConversationHistory({
  conversations,
  contextName,
  disabled,
  onSelect,
}: {
  conversations: ConversationSummary[];
  contextName: string | null;
  disabled: boolean;
  onSelect: (conversationId: string) => void;
}) {
  const listId = useId();

  // One empty conversation is the starting state, not history worth offering.
  const worthShowing = conversations.length > 1 || conversations[0]?.messageCount;
  if (!worthShowing) return null;

  return (
    <details className={styles.wrapper}>
      <summary className={styles.summary}>
        Conversations
        <span className={styles.count}>{conversations.length}</span>
      </summary>

      <div className={styles.panel} id={listId}>
        <p className={styles.scope}>
          {contextName ? `In context: ${contextName}` : "Current context only"}
        </p>
        <ul className={styles.list}>
          {conversations.map((conversation) => (
            <li key={conversation.id}>
              <button
                type="button"
                className={styles.item}
                data-active={conversation.isActive || undefined}
                aria-current={conversation.isActive ? "true" : undefined}
                disabled={disabled}
                onClick={() => onSelect(conversation.id)}
              >
                <span className={styles.title}>
                  {conversation.title ?? "New conversation"}
                </span>
                <span className={styles.meta}>
                  {conversation.isActive && (
                    <span className={styles.current}>Current</span>
                  )}
                  {conversation.messageCount > 0 && (
                    <span className={styles.messages}>
                      {conversation.messageCount}{" "}
                      {conversation.messageCount === 1 ? "message" : "messages"}
                    </span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </details>
  );
}

"use client";

import styles from "./WorkspaceSwitcher.module.css";

import type { Workspace } from "@/lib/auth-types";

/**
 * Which workspace the assistant is currently acting in, and how to change it.
 *
 * Switching is a *server* operation: selecting here calls
 * `POST /api/workspaces/{id}/activate`, which re-checks membership and writes
 * the choice to the session row. This control cannot put the app into a
 * workspace the user does not belong to, because the list it renders comes from
 * the server's own membership query and the server re-checks anyway.
 *
 * The role is shown next to the name because the same person can hold different
 * roles in different workspaces, and "why can't I do this here" is otherwise an
 * invisible difference.
 */
export function WorkspaceSwitcher({
  workspaces,
  activeWorkspace,
  busy,
  onSwitch,
  onManage,
  canManageMembers,
}: {
  workspaces: Workspace[];
  activeWorkspace: Workspace | null;
  busy: boolean;
  onSwitch(workspaceId: string): void;
  onManage(): void;
  canManageMembers: boolean;
}) {
  if (!activeWorkspace) return null;

  const single = workspaces.length <= 1;

  return (
    <div className={styles.wrap}>
      <span className={styles.label}>Workspace</span>

      {single ? (
        <span className={styles.static} title={activeWorkspace.slug}>
          {activeWorkspace.name}
        </span>
      ) : (
        <select
          className={styles.select}
          value={activeWorkspace.workspace_id}
          disabled={busy}
          aria-label="Active workspace"
          onChange={(event) => {
            if (event.target.value !== activeWorkspace.workspace_id) {
              onSwitch(event.target.value);
            }
          }}
        >
          {workspaces.map((workspace) => (
            <option key={workspace.workspace_id} value={workspace.workspace_id}>
              {workspace.name}
            </option>
          ))}
        </select>
      )}

      {activeWorkspace.role ? (
        <span className={styles.role}>{activeWorkspace.role}</span>
      ) : null}

      {/* Hidden when the role does not grant it — a rendering decision only.
          The server refuses the request regardless of what this button does. */}
      {canManageMembers ? (
        <button className={styles.manage} type="button" onClick={onManage}>
          Members
        </button>
      ) : null}
    </div>
  );
}

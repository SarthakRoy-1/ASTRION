"use client";

import { StatusPill } from "./StatusPill";
import { roleLabel } from "@/lib/workspace-presentation";
import type { Workspace } from "@/lib/auth-types";

import styles from "./WorkspaceSwitcher.module.css";

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
 *
 * A native `<select>` rather than a scripted menu: it is keyboard-operable and
 * announced correctly for free, and on a phone it opens the platform's own
 * picker instead of a list this code would have to make scrollable itself.
 */
export function WorkspaceSwitcher({
  workspaces,
  activeWorkspace,
  busy,
  onSwitch,
}: {
  workspaces: Workspace[];
  activeWorkspace: Workspace | null;
  busy: boolean;
  onSwitch(workspaceId: string): void;
}) {
  if (!activeWorkspace) return null;

  const single = workspaces.length <= 1;

  return (
    <div className={styles.wrap}>
      <span className={styles.label}>Workspace</span>

      {single ? (
        <span className={styles.name}>{activeWorkspace.name}</span>
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
              {optionLabel(workspace, workspaces)}
            </option>
          ))}
        </select>
      )}

      {activeWorkspace.role ? (
        <StatusPill tone="neutral" quiet>
          {roleLabel(activeWorkspace.role)}
        </StatusPill>
      ) : null}
    </div>
  );
}

/**
 * A label that identifies one workspace among the caller's own.
 *
 * Two workspaces may legitimately share a name — the same team spins up a
 * second one, or an operator bootstraps one that already existed. Rendering
 * both as "Northstar Logistics" makes the switcher a coin toss, and switching
 * tenant by accident is the one mistake this control must not enable. The slug
 * is appended only where it disambiguates, so the ordinary case stays clean.
 */
function optionLabel(workspace: Workspace, all: Workspace[]): string {
  const duplicated =
    all.filter((other) => other.name === workspace.name).length > 1;
  return duplicated ? `${workspace.name} (${workspace.slug})` : workspace.name;
}

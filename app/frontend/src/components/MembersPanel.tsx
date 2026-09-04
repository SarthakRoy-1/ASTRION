"use client";

import { useCallback, useEffect, useState } from "react";

import styles from "./MembersPanel.module.css";

import {
  changeMemberRole,
  inviteMember,
  listInvitations,
  listMembers,
  removeMember,
  revokeInvitation,
} from "@/lib/auth-client";
import {
  ASSIGNABLE_ROLES,
  ROLE_DESCRIPTIONS,
  type Invitation,
  type Workspace,
  type WorkspaceMember,
  type WorkspaceRole,
} from "@/lib/auth-types";
import { ApiError } from "@/lib/client";

/**
 * Member list, role changes, removals and invitations for one workspace.
 *
 * Controls are hidden when the caller's role does not grant them — but that is
 * a *rendering* decision and nothing more. Every action here is re-authorized
 * on the server, which also enforces the rules this UI cannot sensibly express:
 * that you may not change your own role, may not act on someone at or above
 * your own level, and may not remove the last owner. When the server refuses,
 * its explanation is shown verbatim rather than second-guessed.
 *
 * The invitation token is displayed once, at creation, because this deployment
 * has no mail transport. It is never stored and never re-fetched — the server
 * keeps only a digest, so there is nowhere to fetch it back from.
 */
export function MembersPanel({
  workspace,
  currentUserId,
  onClose,
  onChanged,
}: {
  workspace: Workspace;
  currentUserId: string;
  onClose(): void;
  onChanged(): void;
}) {
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [ownerCount, setOwnerCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [issuedToken, setIssuedToken] = useState<string | null>(null);

  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<WorkspaceRole>("support");

  const permissions = workspace.permissions ?? [];
  const can = (permission: string) => permissions.includes(permission);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const memberListing = await listMembers(workspace.workspace_id);
      setMembers(memberListing.members);
      setOwnerCount(memberListing.owner_count);

      // Only fetched when the role permits it; asking anyway would produce a
      // 403 the user cannot act on and did not ask for.
      if (can("members.invite")) {
        const invitationListing = await listInvitations(workspace.workspace_id);
        setInvitations(invitationListing.invitations);
      } else {
        setInvitations([]);
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
    // `permissions` is derived from `workspace`, which is in the dep list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspace]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(work: () => Promise<unknown>) {
    setError(null);
    try {
      await work();
      await load();
      onChanged();
    } catch (cause) {
      // The backend's wording is the accurate one — it knows which rule was
      // broken. Replacing it with something friendlier would lose that.
      setError(cause instanceof ApiError ? cause.message : String(cause));
    }
  }

  return (
    <section className={styles.panel} aria-label="Workspace members">
      <header className={styles.header}>
        <div>
          <h2 className={styles.title}>{workspace.name}</h2>
          <p className={styles.subtitle}>
            {members.length} member{members.length === 1 ? "" : "s"} ·{" "}
            {ownerCount} owner{ownerCount === 1 ? "" : "s"}
          </p>
        </div>
        <button className={styles.close} type="button" onClick={onClose}>
          Close
        </button>
      </header>

      {error ? <p className={styles.error}>{error}</p> : null}

      {loading ? (
        <p className={styles.muted}>Loading…</p>
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Member</th>
              <th>Role</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {members.map((member) => {
              const isSelf = member.user_id === currentUserId;
              const isOwner = member.role === "owner";
              // Self and owners are excluded here because the server refuses
              // both; offering the control would only produce an error.
              const mayReRole =
                can("members.change_role") && !isSelf && !isOwner;
              const mayRemove =
                (can("members.remove") && !isSelf && !isOwner) || isSelf;

              return (
                <tr key={member.user_id}>
                  <td>
                    <span className={styles.name}>
                      {member.display_name}
                      {isSelf ? <span className={styles.you}> (you)</span> : null}
                    </span>
                    <span className={styles.email}>{member.email}</span>
                  </td>
                  <td>
                    {mayReRole ? (
                      <select
                        className={styles.select}
                        value={member.role}
                        aria-label={`Role for ${member.display_name}`}
                        onChange={(event) =>
                          act(() =>
                            changeMemberRole(
                              workspace.workspace_id,
                              member.user_id,
                              event.target.value as WorkspaceRole,
                            ),
                          )
                        }
                      >
                        {ASSIGNABLE_ROLES.map((role) => (
                          <option key={role} value={role}>
                            {role}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className={styles.roleBadge}>{member.role}</span>
                    )}
                  </td>
                  <td className={styles.actions}>
                    {mayRemove ? (
                      <button
                        className={styles.danger}
                        type="button"
                        onClick={() =>
                          act(() =>
                            removeMember(workspace.workspace_id, member.user_id),
                          )
                        }
                      >
                        {isSelf ? "Leave" : "Remove"}
                      </button>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {can("members.invite") ? (
        <>
          <h3 className={styles.sectionTitle}>Invite a member</h3>
          <form
            className={styles.inviteForm}
            onSubmit={(event) => {
              event.preventDefault();
              setIssuedToken(null);
              void act(async () => {
                const invitation = await inviteMember(
                  workspace.workspace_id,
                  inviteEmail,
                  inviteRole,
                );
                setInviteEmail("");
                if (invitation.invitation_token) {
                  setIssuedToken(invitation.invitation_token);
                }
              });
            }}
          >
            <input
              className={styles.input}
              type="email"
              value={inviteEmail}
              onChange={(event) => setInviteEmail(event.target.value)}
              placeholder="colleague@example.com"
              aria-label="Email address to invite"
              required
            />
            <select
              className={styles.select}
              value={inviteRole}
              aria-label="Role for the invitee"
              onChange={(event) =>
                setInviteRole(event.target.value as WorkspaceRole)
              }
            >
              {ASSIGNABLE_ROLES.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
            <button className={styles.primary} type="submit">
              Invite
            </button>
          </form>
          <p className={styles.hint}>{ROLE_DESCRIPTIONS[inviteRole]}</p>

          {issuedToken ? (
            <div className={styles.tokenNotice}>
              <p className={styles.tokenLede}>
                This deployment has no email delivery, so send this invitation
                link yourself. It is shown once and cannot be retrieved again.
              </p>
              <code className={styles.token}>{issuedToken}</code>
            </div>
          ) : null}

          {invitations.length > 0 ? (
            <>
              <h3 className={styles.sectionTitle}>Pending invitations</h3>
              <ul className={styles.invitations}>
                {invitations.map((invitation) => (
                  <li key={invitation.invitation_id} className={styles.invitation}>
                    <span className={styles.email}>{invitation.email}</span>
                    <span className={styles.roleBadge}>{invitation.role}</span>
                    <span className={styles.status}>{invitation.status}</span>
                    <button
                      className={styles.danger}
                      type="button"
                      onClick={() =>
                        act(() =>
                          revokeInvitation(
                            workspace.workspace_id,
                            invitation.invitation_id,
                          ),
                        )
                      }
                    >
                      Revoke
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

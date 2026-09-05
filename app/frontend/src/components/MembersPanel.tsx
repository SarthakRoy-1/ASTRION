"use client";

import { useCallback, useEffect, useState } from "react";

import { StatusPill } from "./StatusPill";
import { Button } from "./ui/Button";
import { Callout } from "./ui/Callout";
import { Dialog } from "./ui/Dialog";
import { SelectField, TextField } from "./ui/Field";
import { Panel } from "./ui/Panel";
import { SkeletonRows } from "./ui/Loading";
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
import {
  invitationStatusLabel,
  roleLabel,
} from "@/lib/workspace-presentation";

import styles from "./MembersPanel.module.css";

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
 * keeps only a digest, so there is nowhere to fetch it back from. It is now
 * offered as a link to `/join`, which is the screen that actually accepts it;
 * before, the token was printed with nowhere to use it.
 *
 * Removing someone goes through a confirmation dialog. It is the one action on
 * this screen that cannot be undone from this screen — re-adding a member
 * means issuing a fresh invitation and waiting for them to accept it.
 */
export function MembersPanel({
  workspace,
  currentUserId,
  onChanged,
}: {
  workspace: Workspace;
  currentUserId: string;
  onChanged(): void;
}) {
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [ownerCount, setOwnerCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ email: string; token: string } | null>(
    null,
  );
  const [confirming, setConfirming] = useState<WorkspaceMember | null>(null);

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
    <>
      <Panel
        title="Members"
        description={
          loading
            ? "Loading the member list…"
            : `${members.length} member${members.length === 1 ? "" : "s"}, ${ownerCount} owner${ownerCount === 1 ? "" : "s"}. A person's role decides what they can do in this workspace and nowhere else.`
        }
      >
        {error ? (
          <Callout tone="fail" role="alert" title="That did not work">
            {error}
          </Callout>
        ) : null}

        {loading ? (
          <SkeletonRows rows={3} label="Loading the member list." />
        ) : (
          <ul className={styles.list}>
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
                <li key={member.user_id} className={styles.member}>
                  <span className={styles.identity}>
                    <span className={styles.name}>
                      {member.display_name}
                      {isSelf ? <span className={styles.you}>(you)</span> : null}
                    </span>
                    <span className={styles.email}>{member.email}</span>
                  </span>

                  <span className={styles.roleCell}>
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
                            {roleLabel(role)}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <StatusPill tone="neutral" quiet>
                        {roleLabel(member.role)}
                      </StatusPill>
                    )}
                  </span>

                  <span className={styles.actions}>
                    {mayRemove ? (
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => setConfirming(member)}
                      >
                        {isSelf ? "Leave" : "Remove"}
                      </Button>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>

      {can("members.invite") ? (
        <Panel
          className={styles.section}
          title="Invite someone"
          description="An invitation is issued to one address and one role. It can only be accepted by someone signed in as that address."
        >
          <form
            className={styles.inviteForm}
            onSubmit={(event) => {
              event.preventDefault();
              setIssued(null);
              const email = inviteEmail;
              void act(async () => {
                const invitation = await inviteMember(
                  workspace.workspace_id,
                  email,
                  inviteRole,
                );
                setInviteEmail("");
                if (invitation.invitation_token) {
                  setIssued({ email, token: invitation.invitation_token });
                }
              });
            }}
          >
            <TextField
              className={styles.inviteEmail}
              label="Email address"
              type="email"
              value={inviteEmail}
              onChange={(event) => setInviteEmail(event.target.value)}
              placeholder="colleague@example.com"
              required
            />
            <SelectField
              className={styles.inviteRole}
              label="Role"
              value={inviteRole}
              hint={ROLE_DESCRIPTIONS[inviteRole]}
              onChange={(event) =>
                setInviteRole(event.target.value as WorkspaceRole)
              }
            >
              {ASSIGNABLE_ROLES.map((role) => (
                <option key={role} value={role}>
                  {roleLabel(role)}
                </option>
              ))}
            </SelectField>
            <Button type="submit" variant="primary" className={styles.inviteSubmit}>
              Send invitation
            </Button>
          </form>

          {issued ? (
            <div className={styles.tokenNotice}>
              <Callout tone="info" title="Send this link yourself">
                This deployment has no email delivery, so the invitation link is
                shown here once and cannot be retrieved again — the server keeps
                only a digest of it. Give it to {issued.email}, who must be
                signed in as that address to accept.
                <code className={styles.token}>
                  {joinUrl(issued.token)}
                </code>
              </Callout>
            </div>
          ) : null}

          {invitations.length > 0 ? (
            <div className={styles.section}>
              <h3 className={styles.sectionTitle}>Outstanding invitations</h3>
              <ul className={styles.invitations}>
                {invitations.map((invitation) => (
                  <li key={invitation.invitation_id} className={styles.invitation}>
                    <span className={styles.invitationEmail}>
                      {invitation.email}
                    </span>
                    <StatusPill tone="neutral" quiet>
                      {roleLabel(invitation.role)}
                    </StatusPill>
                    <StatusPill
                      tone={invitation.status === "pending" ? "caution" : "neutral"}
                      quiet
                    >
                      {invitationStatusLabel(invitation.status)}
                    </StatusPill>
                    <Button
                      variant="danger"
                      size="sm"
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
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </Panel>
      ) : null}

      <Panel
        className={styles.section}
        title="What each role can do"
        description="Roles are per workspace. The same person can hold a different one in another workspace, and the server checks the role on every request rather than trusting what this screen drew."
      >
        <ul className={styles.roleReference}>
          {(["viewer", "support", "operations", "admin", "owner"] as const).map(
            (role) => (
              <li key={role} className={styles.roleRow}>
                <span className={styles.roleName}>
                  <StatusPill tone="neutral" quiet>
                    {roleLabel(role)}
                  </StatusPill>
                </span>
                <span>{ROLE_DESCRIPTIONS[role]}</span>
              </li>
            ),
          )}
        </ul>
      </Panel>

      {confirming ? (
        <Dialog
          title={
            confirming.user_id === currentUserId
              ? "Leave this workspace?"
              : `Remove ${confirming.display_name}?`
          }
          confirmLabel={
            confirming.user_id === currentUserId ? "Leave workspace" : "Remove"
          }
          onCancel={() => setConfirming(null)}
          onConfirm={() => {
            const target = confirming;
            setConfirming(null);
            void act(() =>
              removeMember(workspace.workspace_id, target.user_id),
            );
          }}
        >
          {confirming.user_id === currentUserId ? (
            <>
              You will lose access to {workspace.name} — its records, its
              documents and its audit trail — until someone invites you back.
            </>
          ) : (
            <>
              {confirming.display_name} will lose access to {workspace.name}.
              Bringing them back means issuing a fresh invitation and waiting
              for them to accept it. Work they already did stays in the audit
              trail.
            </>
          )}
        </Dialog>
      ) : null}
    </>
  );
}

/**
 * The link an invitee opens.
 *
 * Built in the browser from the current origin, because the backend has no
 * mail transport and therefore no configured frontend URL to put in one. On
 * the server render there is no origin to read, so the bare token is shown and
 * replaced once the component hydrates.
 */
function joinUrl(token: string): string {
  if (typeof window === "undefined") return token;
  return `${window.location.origin}/join?token=${encodeURIComponent(token)}`;
}

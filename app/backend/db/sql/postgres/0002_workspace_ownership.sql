-- 0002 workspace ownership: the workspace becomes the tenant boundary.
--
-- Before: accounts, orders and tickets were one global set keyed by account_id;
-- which workspace could see an account was recorded in organization_accounts,
-- and documents/actions were scoped only through a nullable account_id.
-- After: every business record carries the workspace that owns it (org_id), an
-- account id names a customer *within* a workspace, and documents are either
-- owned by a workspace or are system documents (org_id NULL).
--
-- Written to be correct on a database that already holds rows, because a
-- development database might. A production database provisioned fresh holds
-- none, and every backfill below is then a no-op. Existing ownership is kept:
-- an account's workspace is what organization_accounts said it was; rows nobody
-- owned go to a single, clearly named legacy workspace rather than being
-- dropped or left ownerless.

-- 1. A home for legacy rows, only if there are any.
INSERT INTO organizations (org_id, name, slug, status, created_at_utc)
SELECT 'ORG-legacy-assessment', 'Assessment dataset (legacy)', 'assessment-legacy',
       'active', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')
 WHERE (EXISTS (SELECT 1 FROM accounts)
        OR EXISTS (SELECT 1 FROM dataset_metadata)
        OR EXISTS (SELECT 1 FROM agent_actions)
        OR EXISTS (SELECT 1 FROM documents WHERE account_id IS NOT NULL))
   AND NOT EXISTS (SELECT 1 FROM organizations WHERE org_id = 'ORG-legacy-assessment');

-- 2. accounts: owner, then a composite key.
ALTER TABLE accounts ADD COLUMN org_id TEXT COLLATE "C";
ALTER TABLE accounts ADD COLUMN created_at_utc TEXT COLLATE "C";
UPDATE accounts
   SET org_id = COALESCE(
           (SELECT oa.org_id FROM organization_accounts oa WHERE oa.account_id = accounts.account_id LIMIT 1),
           'ORG-legacy-assessment'),
       created_at_utc = (SELECT oa.created_at_utc FROM organization_accounts oa
                          WHERE oa.account_id = accounts.account_id LIMIT 1);

ALTER TABLE orders ADD COLUMN org_id TEXT COLLATE "C";
UPDATE orders SET org_id = a.org_id FROM accounts a WHERE a.account_id = orders.account_id;
ALTER TABLE tickets ADD COLUMN org_id TEXT COLLATE "C";
UPDATE tickets SET org_id = a.org_id FROM accounts a WHERE a.account_id = tickets.account_id;

ALTER TABLE orders DROP CONSTRAINT orders_account_id_fkey;
ALTER TABLE tickets DROP CONSTRAINT tickets_account_id_fkey;
ALTER TABLE accounts DROP CONSTRAINT accounts_pkey;
ALTER TABLE orders DROP CONSTRAINT orders_pkey;
ALTER TABLE tickets DROP CONSTRAINT tickets_pkey;

ALTER TABLE accounts ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE orders ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE tickets ALTER COLUMN org_id SET NOT NULL;

ALTER TABLE accounts ADD PRIMARY KEY (org_id, account_id);
ALTER TABLE accounts ADD FOREIGN KEY (org_id) REFERENCES organizations (org_id);
ALTER TABLE orders ADD PRIMARY KEY (org_id, order_id);
ALTER TABLE orders ADD FOREIGN KEY (org_id, account_id) REFERENCES accounts (org_id, account_id);
ALTER TABLE tickets ADD PRIMARY KEY (org_id, ticket_id);
ALTER TABLE tickets ADD FOREIGN KEY (org_id, account_id) REFERENCES accounts (org_id, account_id);

-- 3. Provenance follows the record it explains.
ALTER TABLE source_provenance ADD COLUMN org_id TEXT COLLATE "C";
UPDATE source_provenance SET org_id = a.org_id FROM accounts a
 WHERE source_provenance.target_table = 'accounts' AND a.account_id = source_provenance.target_id;
UPDATE source_provenance SET org_id = o.org_id FROM orders o
 WHERE source_provenance.target_table = 'orders' AND o.order_id = source_provenance.target_id;
UPDATE source_provenance SET org_id = t.org_id FROM tickets t
 WHERE source_provenance.target_table = 'tickets' AND t.ticket_id = source_provenance.target_id;
UPDATE source_provenance SET org_id = 'ORG-legacy-assessment' WHERE org_id IS NULL;
ALTER TABLE source_provenance ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE source_provenance ADD FOREIGN KEY (org_id) REFERENCES organizations (org_id);
ALTER TABLE source_provenance DROP CONSTRAINT source_provenance_target_table_target_id_key;
ALTER TABLE source_provenance ADD UNIQUE (org_id, target_table, target_id);

-- 4. The reference snapshot is per workspace (the old single row becomes the
--    legacy workspace's).
ALTER TABLE dataset_metadata ADD COLUMN org_id TEXT COLLATE "C";
UPDATE dataset_metadata SET org_id = 'ORG-legacy-assessment';
ALTER TABLE dataset_metadata DROP COLUMN id;
ALTER TABLE dataset_metadata ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE dataset_metadata ADD PRIMARY KEY (org_id);
ALTER TABLE dataset_metadata ADD FOREIGN KEY (org_id) REFERENCES organizations (org_id);

ALTER TABLE ingestion_runs ADD COLUMN org_id TEXT COLLATE "C" REFERENCES organizations (org_id);
ALTER TABLE document_ingestion_runs ADD COLUMN org_id TEXT COLLATE "C" REFERENCES organizations (org_id);

-- 5. documents: owned by a workspace, or a system document (NULL).
ALTER TABLE documents ADD COLUMN org_id TEXT COLLATE "C" REFERENCES organizations (org_id);
UPDATE documents SET org_id = COALESCE(
           (SELECT a.org_id FROM accounts a WHERE a.account_id = documents.account_id LIMIT 1),
           'ORG-legacy-assessment')
 WHERE account_id IS NOT NULL;
ALTER TABLE documents DROP CONSTRAINT documents_source_file_key;
ALTER TABLE documents ADD CHECK (org_id IS NOT NULL OR account_id IS NULL);

-- 6. Prepared actions and their effects belong to a workspace.
ALTER TABLE agent_actions ADD COLUMN org_id TEXT COLLATE "C";
UPDATE agent_actions SET org_id = COALESCE(
           (SELECT a.org_id FROM accounts a WHERE a.account_id = agent_actions.account_id LIMIT 1),
           'ORG-legacy-assessment');
ALTER TABLE agent_actions ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE agent_actions ADD FOREIGN KEY (org_id) REFERENCES organizations (org_id);

ALTER TABLE ticket_escalations ADD COLUMN org_id TEXT COLLATE "C";
UPDATE ticket_escalations SET org_id = x.org_id FROM agent_actions x WHERE x.action_id = ticket_escalations.action_id;
ALTER TABLE ticket_escalations ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE ticket_escalations ADD FOREIGN KEY (org_id) REFERENCES organizations (org_id);

ALTER TABLE service_credits ADD COLUMN org_id TEXT COLLATE "C";
UPDATE service_credits SET org_id = x.org_id FROM agent_actions x WHERE x.action_id = service_credits.action_id;
ALTER TABLE service_credits ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE service_credits ADD FOREIGN KEY (org_id) REFERENCES organizations (org_id);

ALTER TABLE ticket_notes ADD COLUMN org_id TEXT COLLATE "C";
UPDATE ticket_notes SET org_id = x.org_id FROM agent_actions x WHERE x.action_id = ticket_notes.action_id;
ALTER TABLE ticket_notes ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE ticket_notes ADD FOREIGN KEY (org_id) REFERENCES organizations (org_id);

-- 7. Indexes for the new access paths.
DROP INDEX IF EXISTS idx_orders_account_id;
DROP INDEX IF EXISTS idx_tickets_account_id;
DROP INDEX IF EXISTS idx_documents_account_id;
DROP INDEX IF EXISTS idx_actions_status;
DROP INDEX IF EXISTS idx_actions_target;
CREATE INDEX idx_orders_account ON orders (org_id, account_id);
CREATE INDEX idx_tickets_account ON tickets (org_id, account_id);
CREATE INDEX idx_documents_org ON documents (org_id, account_id);
-- One document per source file per owner. COALESCE because NULL (the system
-- owner) never equals NULL in a unique index.
CREATE UNIQUE INDEX uq_documents_owner_file ON documents (COALESCE(org_id, ''), source_file);
CREATE INDEX idx_actions_status ON agent_actions (org_id, status);
CREATE INDEX idx_actions_target ON agent_actions (org_id, target_type, target_id);

-- 8. organization_accounts: the relationship is now the account row itself.
DROP TABLE organization_accounts;
CREATE VIEW organization_accounts AS
    SELECT org_id, account_id, created_at_utc FROM accounts;

-- 0001_initial / 10: platform rows, tenancy and identity (spec-03 §2). Every tenant table carries
-- CHECK (tenant_id <> '') so an empty app.tenant_id never matches a row (spec-03 §1 conventions).

-- platform (no RLS; noema_app has SELECT on config_version and price_catalog only)
CREATE TABLE platform.config_version (
  version int PRIMARY KEY, digest text NOT NULL, body jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('proposed','active','superseded','rejected')),
  proposed_by text NOT NULL, proposed_at timestamptz NOT NULL DEFAULT now(),
  approved_by text, approved_at timestamptz, activated_at timestamptz,
  CHECK (approved_by IS NULL OR approved_by <> proposed_by));                    -- two-person (IMPL-044)
CREATE UNIQUE INDEX config_one_active ON platform.config_version ((true)) WHERE status = 'active';

CREATE TABLE platform.price_catalog (                                          -- spec-05 §2; prices_ref "db:<version>"
  version int PRIMARY KEY, digest text NOT NULL, body jsonb NOT NULL,
  imported_by text NOT NULL, imported_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE platform.operator (
  id text PRIMARY KEY, name text UNIQUE NOT NULL, key_id text UNIQUE NOT NULL, key_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz);

-- tenancy and identity
CREATE TABLE noema.tenant (
  id text PRIMARY KEY CHECK (id <> ''), aws_account_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz);       -- synced from config on activation

CREATE TABLE noema.project (
  tenant_id text NOT NULL CHECK (tenant_id <> '') REFERENCES noema.tenant(id), id text NOT NULL, name text NOT NULL,
  default_data_class text NOT NULL DEFAULT 'internal'
    CHECK (default_data_class IN ('public','internal','confidential','restricted')),
  created_at timestamptz NOT NULL DEFAULT now(), deleted_at timestamptz,
  PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, name));

CREATE TABLE noema.principal (
  tenant_id text NOT NULL CHECK (tenant_id <> ''), project_id text NOT NULL, id text NOT NULL, subject text NOT NULL,
  roles text[] NOT NULL CHECK (roles <@ ARRAY['tenant_admin','developer','reviewer','end_user','auditor']),
  created_at timestamptz NOT NULL DEFAULT now(), disabled_at timestamptz,
  PRIMARY KEY (tenant_id, id), FOREIGN KEY (tenant_id, project_id) REFERENCES noema.project,
  UNIQUE (tenant_id, project_id, subject));

CREATE TABLE auth.api_key (
  key_id text PRIMARY KEY,                                                     -- globally unique ULID (IMPL-008)
  tenant_id text NOT NULL CHECK (tenant_id <> ''), project_id text NOT NULL, principal_id text NOT NULL,
  key_hash text NOT NULL, pepper_version int NOT NULL, env text NOT NULL CHECK (env IN ('live','test')),
  scopes text[] NOT NULL
    CHECK (scopes <@ ARRAY['turns','decisions','sessions','files.read','files.write','usage.read','admin.read']),
  ip_allowlist cidr[], expires_at timestamptz, revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz,
  FOREIGN KEY (tenant_id, principal_id) REFERENCES noema.principal);

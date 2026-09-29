-- OPS-G (OPS-06): PostgreSQL login of Grafana, a member of analytics_read
-- (backend/migrations/0016_analytics.sql). deploy/grafana/create-reader.sh sends
--   SET infrapulse.reader = '<login>';
--   SET infrapulse.reader_password = '<secret from the server .env>';
-- followed by this file to psql in the db container, so the password is never kept
-- in Git or passed on a command line. Re-running updates password and settings.
--
-- A group role's settings are not inherited, so the read-only transaction mode, the
-- statement timeout and the connection limit are set on the login itself. The
-- script refuses a role with extra powers, other memberships or owned objects
-- (for example the application user), so it never turns such a role read-only.
DO $$
DECLARE
  reader text := current_setting('infrapulse.reader');
  secret text := current_setting('infrapulse.reader_password');
  existing pg_roles%ROWTYPE;
BEGIN
  IF reader !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
    RAISE EXCEPTION 'reader login must match [a-z_][a-z0-9_]{0,62}';
  END IF;
  IF length(secret) < 24 THEN
    RAISE EXCEPTION 'reader password must have at least 24 characters';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_read') THEN
    RAISE EXCEPTION 'role analytics_read is missing: apply migration 0016 first';
  END IF;
  IF reader = current_user OR reader = 'analytics_read' THEN
    RAISE EXCEPTION 'reader login must be a separate role';
  END IF;
  SELECT * INTO existing FROM pg_roles WHERE rolname = reader;
  IF FOUND THEN
    IF existing.rolsuper OR existing.rolcreaterole OR existing.rolcreatedb
       OR existing.rolreplication OR existing.rolbypassrls THEN
      RAISE EXCEPTION 'refusing to use the privileged role % as the Grafana reader', reader;
    END IF;
    IF EXISTS (
      SELECT 1 FROM pg_auth_members AS membership
      JOIN pg_roles AS parent ON parent.oid = membership.roleid
      WHERE membership.member = existing.oid AND parent.rolname <> 'analytics_read'
    ) OR EXISTS (SELECT 1 FROM pg_class WHERE relowner = existing.oid)
      OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = existing.oid) THEN
      RAISE EXCEPTION 'role % has other memberships or owns objects; choose another login', reader;
    END IF;
  ELSE
    EXECUTE format('CREATE ROLE %I LOGIN', reader);
  END IF;
  EXECUTE format(
    'ALTER ROLE %I WITH LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE '
    'NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5 PASSWORD %L',
    reader, secret
  );
  EXECUTE format('GRANT analytics_read TO %I', reader);
  EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', reader);
  EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', reader, '10s');
  EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', reader, '60s');
  RAISE NOTICE 'Grafana reader % is ready (member of analytics_read)', reader;
END $$;

RESET infrapulse.reader_password;
RESET infrapulse.reader;

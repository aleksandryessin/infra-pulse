-- Runtime role of the API and the ingestion worker (SEC-03, SEC-04; 29.09.2026).
--
-- infra_pulse_app is the one PostgreSQL role the HTTP API, the admin CLI in the api
-- container and the ingestion worker connect with. The migration user (POSTGRES_USER,
-- owner of the schema) keeps DDL, migrations, backups and one-off loads. The runtime
-- role gets CONNECT and TEMPORARY on the database (the worker stages files in temp
-- tables), USAGE on the schema and, per relation, only the DML listed below:
--   * no CREATE on the database or the schema, no TRUNCATE, REFERENCES, TRIGGER or
--     MAINTAIN on any table, nothing in the schema `analytics` (Grafana, 0016);
--   * append-only history: audit_events, forecast_decisions, forecast_check_results,
--     work_order_drafts and replay_review_audit get INSERT and SELECT only, so the
--     triggers of 0014/0019 are no longer the only guard and TRUNCATE is refused;
--   * dispatch_observations: the text of a record is never rewritten, UPDATE covers
--     only the source alarm flag («не передан» -> true/false, 0018).
-- Relations of this schema without a decision below get no privilege and a WARNING;
-- backend/tests/test_db_roles.py fails for such a table, so every new table needs an
-- explicit line here. Sequences follow their table: USAGE when the table has INSERT.
--
-- The 9000 prefix sorts after every schema migration, and every file is applied on each
-- run, so this file always sees the tables of the current revision and re-applies the
-- whole matrix (REVOKE ALL, then GRANT) in the migration transaction.
--
-- The role is created NOLOGIN and without a password. LOGIN and the password (a
-- SCRAM verifier computed on the client) are set after the migrations by
-- backend/scripts/migrate_operational_db.py from INFRA_DB_APP_PASSWORD of the server
-- .env (deploy/remote-deploy.sh generates it); nothing secret is kept here. Roles are
-- global to the cluster: this file never drops the role and never changes its LOGIN
-- or password. Without CREATEROLE the tables are still migrated and the role is left
-- to a DBA with a WARNING, like analytics_read in 0016.
DO $$
DECLARE
  runtime CONSTANT name := 'infra_pulse_app';
  -- One line per relation of the schema: privileges of the runtime role ('' = none).
  decisions CONSTANT text[] := ARRAY[
    -- observations and scopes (0001-0011, 0018): worker loads, API reads and review notes
    ['dispatch_replay_snapshots',        'SELECT, INSERT, UPDATE'],
    ['dispatch_observations',            'SELECT, INSERT, UPDATE (alarm)'],
    ['dispatch_replay_channel_roster',   'SELECT'],
    ['dispatch_received_inbox_failures', 'SELECT'],
    ['dispatch_received_batches',        ''],
    ['replay_review_notes',              'SELECT, INSERT'],
    ['replay_review_audit',              'SELECT, INSERT'],
    -- uploads, queue and references (0012, 0015)
    ['import_files',                     'SELECT, INSERT, UPDATE'],
    ['import_quarantine',                'SELECT, INSERT'],
    ['jobs',                             'SELECT, INSERT, UPDATE'],
    ['ref_versions',                     'SELECT, INSERT'],
    ['ref_channels',                     'SELECT, INSERT'],
    ['ref_objects',                      'SELECT, INSERT'],
    ['ref_states',                       'SELECT, INSERT'],
    ['observation_batches',              'SELECT, INSERT'],
    ['integration_tokens',               'SELECT, INSERT, UPDATE'],
    -- forecast publication (0013): recomputed by the worker, read by the API
    ['forecast_scopes',                  'SELECT, INSERT, UPDATE'],
    ['forecast_channel_layout',          'SELECT, INSERT, UPDATE'],
    ['forecast_objects',                 'SELECT, INSERT, UPDATE'],
    ['forecast_detector_state',          'SELECT, INSERT, UPDATE'],
    ['forecast_phase_episodes',          'SELECT, INSERT, UPDATE, DELETE'],
    ['forecast_phase_candidates',        'SELECT, INSERT, DELETE'],
    ['forecast_phase_events',            'SELECT, INSERT, DELETE'],
    ['forecast_coverage_days',           'SELECT, INSERT, UPDATE'],
    ['forecast_object_days',             'SELECT, INSERT, UPDATE'],
    ['forecast_runs',                    'SELECT, INSERT'],
    ['forecast_cutoffs',                 'SELECT, INSERT'],
    ['forecast_cards',                   'SELECT, INSERT'],
    ['forecast_card_outcomes',           'SELECT, INSERT, UPDATE'],
    ['forecast_card_window_events',      'SELECT, INSERT, DELETE'],
    ['forecast_card_log',                'SELECT, INSERT, UPDATE'],
    -- sessions, dispatcher work and the action journal (0014, 0019)
    ['auth_sessions',                    'SELECT, INSERT, UPDATE, DELETE'],
    ['forecast_decisions',               'SELECT, INSERT'],
    ['forecast_check_results',           'SELECT, INSERT'],
    ['work_order_drafts',                'SELECT, INSERT'],
    ['audit_events',                     'SELECT, INSERT']
  ];
  target_schema name := current_schema();
  schema_oid oid;
  is_super boolean;
  can_manage boolean;
  decision text[];
  granted jsonb := '{}';
  relation record;
  sequence_row record;
  privileges text;
BEGIN
  IF target_schema IS NULL THEN
    RAISE EXCEPTION 'no current schema: set search_path before applying migrations';
  END IF;
  SELECT oid INTO schema_oid FROM pg_namespace WHERE nspname = target_schema;
  SELECT rolsuper, rolsuper OR rolcreaterole INTO is_super, can_manage
  FROM pg_roles WHERE rolname = current_user;
  IF current_user = runtime THEN
    RAISE EXCEPTION 'apply migrations as the schema owner (POSTGRES_USER), not as %', runtime;
  END IF;

  -- Only DML privileges from the fixed list above ever reach GRANT.
  FOREACH decision SLICE 1 IN ARRAY decisions LOOP
    IF decision[2] <> '' AND decision[2] !~
       '^(SELECT|INSERT|DELETE|UPDATE( \([a-z_]+(, [a-z_]+)*\))?)(, (SELECT|INSERT|DELETE|UPDATE( \([a-z_]+(, [a-z_]+)*\))?))*$' THEN
      RAISE EXCEPTION 'invalid runtime privileges % for %', decision[2], decision[1];
    END IF;
    IF granted ? decision[1] THEN
      RAISE EXCEPTION 'two runtime privilege decisions for %', decision[1];
    END IF;
    granted := granted || jsonb_build_object(decision[1], decision[2]);
  END LOOP;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = runtime) THEN
    IF NOT can_manage THEN
      RAISE WARNING 'role % is missing and % cannot create roles: create it as a DBA '
        '(deploy/README.md «Роли PostgreSQL»)', runtime, current_user;
      RETURN;
    END IF;
    BEGIN
      EXECUTE format('CREATE ROLE %I NOLOGIN', runtime);
    EXCEPTION WHEN duplicate_object THEN
      NULL;  -- created by a concurrent migration run
    END;
  END IF;
  -- LOGIN and the password are left as they are (deploy sets them).
  IF can_manage THEN
    BEGIN
      EXECUTE format(
        'ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT',
        runtime
      );
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE WARNING 'cannot normalise the attributes of role %: %', runtime, SQLERRM;
    END;
  END IF;

  IF is_super OR EXISTS (
    SELECT 1 FROM pg_database
    WHERE datname = current_database() AND pg_has_role(current_user, datdba, 'USAGE')
  ) THEN
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', current_database(), runtime);
  ELSE
    RAISE WARNING '% does not own database %: CONNECT and TEMPORARY for % come from PUBLIC',
      current_user, current_database(), runtime;
  END IF;

  IF is_super OR EXISTS (
    SELECT 1 FROM pg_namespace
    WHERE oid = schema_oid AND pg_has_role(current_user, nspowner, 'USAGE')
  ) THEN
    -- The PostgreSQL 15+ default, also on older servers: nobody but the owner creates here.
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM PUBLIC', target_schema);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', target_schema, runtime);
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', target_schema, runtime);
  ELSE
    RAISE WARNING '% does not own schema %: USAGE for % is left unchanged',
      current_user, target_schema, runtime;
  END IF;

  FOR relation IN
    SELECT c.relname, pg_has_role(current_user, c.relowner, 'USAGE') AS owned
    FROM pg_class AS c
    WHERE c.relnamespace = schema_oid AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    ORDER BY c.relname
  LOOP
    IF NOT (is_super OR relation.owned) THEN
      RAISE WARNING '% does not own %.%: privileges of % unchanged',
        current_user, target_schema, relation.relname, runtime;
      CONTINUE;
    END IF;
    EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM %I', target_schema, relation.relname, runtime);
    privileges := granted ->> relation.relname;
    IF privileges IS NULL THEN
      RAISE WARNING '%.% has no runtime privilege decision in 9000_runtime_role.sql: % gets no access',
        target_schema, relation.relname, runtime;
    ELSIF privileges <> '' THEN
      EXECUTE format('GRANT %s ON TABLE %I.%I TO %I',
        privileges, target_schema, relation.relname, runtime);
    END IF;
  END LOOP;

  FOR relation IN
    SELECT key AS relname FROM jsonb_object_keys(granted) AS key
    WHERE NOT EXISTS (
      SELECT 1 FROM pg_class AS c WHERE c.relnamespace = schema_oid AND c.relname = key
    )
    ORDER BY 1
  LOOP
    RAISE WARNING 'runtime privilege decision for %.%, which does not exist', target_schema, relation.relname;
  END LOOP;

  -- Sequences behind serial columns: nextval() of an INSERT needs USAGE.
  FOR sequence_row IN
    SELECT s.relname AS sequence_name, t.relname AS table_name,
           pg_has_role(current_user, s.relowner, 'USAGE') AS owned
    FROM pg_class AS s
    LEFT JOIN pg_depend AS d
      ON d.classid = 'pg_class'::regclass AND d.objid = s.oid
     AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
    LEFT JOIN pg_class AS t ON t.oid = d.refobjid
    WHERE s.relkind = 'S' AND s.relnamespace = schema_oid
    ORDER BY s.relname
  LOOP
    IF NOT (is_super OR sequence_row.owned) THEN
      CONTINUE;
    END IF;
    EXECUTE format('REVOKE ALL ON SEQUENCE %I.%I FROM %I',
      target_schema, sequence_row.sequence_name, runtime);
    IF coalesce(granted ->> sequence_row.table_name, '') ~ '\mINSERT\M' THEN
      EXECUTE format('GRANT USAGE ON SEQUENCE %I.%I TO %I',
        target_schema, sequence_row.sequence_name, runtime);
    END IF;
  END LOOP;
END $$;

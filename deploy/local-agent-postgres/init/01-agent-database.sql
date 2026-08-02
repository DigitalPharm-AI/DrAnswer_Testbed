CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_migrator') THEN
        CREATE ROLE agent_migrator LOGIN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app_rw') THEN
        CREATE ROLE agent_app_rw LOGIN;
    END IF;
END
$$;

ALTER DATABASE dranswer_agent OWNER TO agent_migrator;
GRANT CONNECT ON DATABASE dranswer_agent TO agent_app_rw;
GRANT USAGE ON SCHEMA public TO agent_app_rw;

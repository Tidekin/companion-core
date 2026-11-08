\getenv core_password DEV_CORE_PASSWORD
\getenv archive_password DEV_ARCHIVE_PASSWORD
CREATE ROLE dev_core LOGIN PASSWORD :'core_password';
CREATE ROLE dev_archive LOGIN CREATEDB PASSWORD :'archive_password';
CREATE DATABASE companion OWNER dev_core;
CREATE DATABASE dev_restore_before OWNER dev_archive;
CREATE DATABASE dev_restore_after OWNER dev_archive;

\connect companion
ALTER DEFAULT PRIVILEGES FOR ROLE dev_core IN SCHEMA public
    GRANT SELECT ON TABLES TO dev_archive;
ALTER DEFAULT PRIVILEGES FOR ROLE dev_core IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO dev_archive;

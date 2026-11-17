"""Trusted reset of a dedicated PostgreSQL slot; no production connection."""

import secrets
from contextlib import closing
from urllib.parse import quote

from .slot_network import address


class Database:
    def __init__(self, host, password):
        if host not in {"agent-database", "validation-database"}:
            raise ValueError("Choose a dedicated test database service.")
        self.host = address(host)
        self.password = password

    def connect(self):
        import psycopg2

        connection = psycopg2.connect(
            host=self.host,
            user="postgres",
            password=self.password,
            dbname="postgres",
            connect_timeout=5,
        )
        connection.autocommit = True
        return connection

    def reset(self, _operation_id):
        from psycopg2 import sql

        with closing(self.connect()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'task' AND pid <> pg_backend_pid()"
                )
                cursor.execute(
                    "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"
                )
                for (name,) in cursor.fetchall():
                    cursor.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(name)
                        )
                    )
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = 'task'")
                if cursor.fetchone():
                    cursor.execute("DROP OWNED BY task CASCADE")
                    cursor.execute("DROP ROLE task")

    def prepare(self, operation_id):
        self.reset(operation_id)
        password = secrets.token_hex(24)
        with closing(self.connect()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE ROLE task LOGIN NOSUPERUSER NOCREATEROLE NOREPLICATION NOINHERIT CREATEDB CONNECTION LIMIT 32 PASSWORD %s",
                    (password,),
                )
                cursor.execute("CREATE DATABASE task OWNER task")
        return f"postgresql://task:{quote(password)}@{self.host}:5432/task"

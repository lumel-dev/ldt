"""`ldt db` — consultar la base del proyecto sin psql y sin abrir un cliente.

Pensado para proyectos con PostgreSQL, donde la URL de conexion esta en el
entorno del proyecto, asi que el comando la resuelve solo desde el cwd.

Por defecto es de solo lectura: cualquier sentencia que escriba necesita --write, para
que un agente no pueda modificar datos por accidente al "mirar" algo.
"""

from __future__ import annotations

import re
from pathlib import Path

from ldt import core

URL_KEYS = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_URL_NON_POOLING",
    "DATABASE_URL_UNPOOLED",
    "DB_URL",
    "PG_URL",
    "NEON_DATABASE_URL",
    "SUPABASE_DB_URL",
)

WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|drop|truncate|alter|create|grant|revoke|copy|vacuum|reindex|call|do)\b",
    re.I,
)


def resolve_url(a) -> tuple[str, str]:
    if a.url:
        return a.url, "--url"
    root = core.find_root(Path(a.cwd))
    env = core.project_env(root)
    key = a.key or next((k for k in URL_KEYS if env.get(k)), None)
    if not key or not env.get(key):
        core.die(
            "no encontre la URL de la base. Pasala con --url, o indicá la variable con --key. "
            f"Buscadas: {', '.join(URL_KEYS)}"
        )
    return env[key], key


def fix_ssl(url: str) -> str:
    """Usa el almacen de certificados del sistema cuando la URL pide verificacion.

    Neon y Supabase mandan `sslmode=verify-full`, que en libpq busca un root.crt en el
    perfil del usuario y en Windows no existe: la conexion falla aunque las credenciales
    esten bien. Se apunta al bundle de certifi, que si esta. La verificacion sigue activa
    (no se baja a `require`), solo cambia de donde salen los certificados raiz.
    """
    if "sslmode=verify" not in url or "sslrootcert=" in url:
        return url
    try:
        import certifi

        bundle = certifi.where()
    except ImportError:
        bundle = "system"
    return url + ("&" if "?" in url else "?") + f"sslrootcert={bundle}"


def connect(a):
    try:
        import psycopg2
    except ImportError:
        core.die("falta psycopg2: pip install psycopg2-binary")
    url, source = resolve_url(a)
    conn = psycopg2.connect(fix_ssl(url), connect_timeout=10)
    conn.set_session(readonly=not getattr(a, "write", False), autocommit=True)
    return conn, source


def run_sql(a, sql: str, params=None) -> tuple[list[dict], str | None]:
    import psycopg2.extras

    conn, _ = connect(a)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            note = cur.statusmessage
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        return rows, note
    finally:
        conn.close()


def cmd_query(a):
    if WRITE_RE.match(a.sql) and not a.write:
        core.die("esa sentencia escribe. Repetila con --write si es a proposito.")
    sql = a.sql
    if a.limit and re.match(r"^\s*select\b", sql, re.I) and " limit " not in sql.lower():
        sql = f"{sql.rstrip().rstrip(';')} LIMIT {a.limit}"
    rows, note = run_sql(a, sql)
    core.out({"rows": rows, "count": len(rows), "status": note}, a.json, core.table(rows) + f"\n({note})")


def cmd_tables(a):
    rows, _ = run_sql(
        a,
        """
        SELECT c.relname AS table,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS size,
               c.reltuples::bigint AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p','v','m') AND n.nspname = %s
        ORDER BY pg_total_relation_size(c.oid) DESC
        """,
        (a.schema,),
    )
    core.out({"tables": rows}, a.json, core.table(rows))


def cmd_schema(a):
    cols, _ = run_sql(
        a,
        """
        SELECT column_name AS column, data_type AS type, is_nullable AS nullable,
               column_default AS default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (a.schema, a.table),
    )
    if not cols:
        core.die(f"no existe la tabla {a.schema}.{a.table}")
    cons, _ = run_sql(
        a,
        """
        SELECT conname AS name, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid = %s::regclass
        ORDER BY contype
        """,
        (f"{a.schema}.{a.table}",),
    )
    idx, _ = run_sql(a, "SELECT indexname AS name, indexdef AS definition FROM pg_indexes WHERE schemaname = %s AND tablename = %s", (a.schema, a.table))
    text = (
        f"{a.schema}.{a.table}\n\n{core.table(cols)}\n\nconstraints:\n{core.table(cons, max_width=90)}"
        f"\n\nindices:\n{core.table(idx, max_width=90)}"
    )
    core.out({"columns": cols, "constraints": cons, "indexes": idx}, a.json, text)


def cmd_ping(a):
    rows, _ = run_sql(a, "SELECT current_database() AS db, current_user AS user, version() AS version")
    _, source = resolve_url(a)
    info = {**rows[0], "url_from": source}
    core.out(info, a.json, f"ok: {info['db']} como {info['user']} (URL de {source})\n{info['version'][:80]}")


def register(sub):
    p = sub.add_parser("db", help="consultar la base PostgreSQL del proyecto (solo lectura por defecto)")
    p.add_argument("--url", help="connection string explicita")
    p.add_argument("--key", help="variable de entorno de donde sacar la URL")
    p.add_argument("--schema", default="public")
    p.add_argument("--write", action="store_true", help="permitir sentencias que escriben")
    ds = p.add_subparsers(dest="db_cmd", required=True)

    sp = ds.add_parser("q", help="correr una consulta")
    sp.add_argument("sql")
    sp.add_argument("--limit", type=int, default=50, help="LIMIT implicito en los SELECT (0 = sin limite)")
    sp.set_defaults(func=cmd_query)

    sp = ds.add_parser("tables", help="listar tablas con tamano y filas aproximadas")
    sp.set_defaults(func=cmd_tables)

    sp = ds.add_parser("schema", help="columnas, constraints e indices de una tabla")
    sp.add_argument("table")
    sp.set_defaults(func=cmd_schema)

    sp = ds.add_parser("ping", help="verificar la conexion y de donde sale la URL")
    sp.set_defaults(func=cmd_ping)

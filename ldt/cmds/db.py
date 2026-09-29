"""`ldt db` — consultar la base del proyecto sin psql y sin abrir un cliente.

Pensado para proyectos con PostgreSQL, donde la URL de conexion esta en el
entorno del proyecto, asi que el comando la resuelve solo desde el cwd.

Por defecto es de solo lectura: cualquier sentencia que escriba necesita --write, para
que un agente no pueda modificar datos por accidente al "mirar" algo.

**Dev por defecto, produccion a pedido.** Muchos proyectos tienen las dos URLs en el mismo
.env (`DATABASE_URL` + `DATABASE_URL_DEV`, y el codigo elige con `ENVIRONMENT`). Antes se
tomaba la primera de URL_KEYS, o sea `DATABASE_URL`, que en esos proyectos es produccion: un
agente que creia estar mirando dev consultaba la base real sin enterarse. Ahora, si existe la
variante de dev de la variable, se usa esa, y la otra hace falta pedirla con `--prod`. No se
mira el `ENVIRONMENT` del .env a proposito: un .env que quedo apuntando a produccion no puede
alcanzar para que un comando de "mirar" vaya ahi.

Cada comando dice a que base fue (host, variable y si es dev o PRODUCCION) por stderr, y
escribir en produccion pide `--write` **y** `--prod`.
"""

from __future__ import annotations

import re
import sys
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

#: Sufijos con los que un proyecto nombra su base de desarrollo, en orden de preferencia.
DEV_SUFFIXES = ("_DEV", "_DEVELOPMENT", "_LOCAL")

WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|drop|truncate|alter|create|grant|revoke|copy|vacuum|reindex|call|do)\b",
    re.I,
)


def resolve_url(a) -> tuple[str, str, str]:
    """(url, variable de donde salio, entorno: "dev" | "PRODUCCION" | "?").

    Sin --key ni --url: si el proyecto tiene la variante de dev de alguna variable conocida
    (`DATABASE_URL_DEV`), se usa esa salvo que se pida --prod. La base "de produccion" sin
    variante de dev queda como "?": no hay con que compararla, y decir "dev" seria mentir.
    """
    if a.url:
        return a.url, "--url", _entorno_de_url(a.url)
    root = core.find_root(Path(a.cwd))
    env = core.project_env(root)
    prod = getattr(a, "prod", False)

    if a.key:
        if not env.get(a.key):
            core.die(f"la variable {a.key} no esta en el entorno del proyecto")
        return env[a.key], a.key, _entorno(a.key, env)

    base = next((k for k in URL_KEYS if env.get(k)), None)
    dev = next(
        (k + suf for k in URL_KEYS for suf in DEV_SUFFIXES if env.get(k + suf)),
        None,
    )
    if prod:
        if not base:
            core.die(f"--prod: no hay ninguna de {', '.join(URL_KEYS)} en el entorno")
        return env[base], base, _entorno(base, env)
    key = dev or base
    if not key:
        core.die(
            "no encontre la URL de la base. Pasala con --url, o indicá la variable con --key. "
            f"Buscadas: {', '.join(URL_KEYS)} (y sus variantes {'/'.join(DEV_SUFFIXES)})"
        )
    return env[key], key, _entorno(key, env)


def _entorno(key: str, env: dict[str, str]) -> str:
    if key.upper().endswith(DEV_SUFFIXES):
        return "dev"
    if any(env.get(key + suf) for suf in DEV_SUFFIXES):
        return "PRODUCCION"  # hay una de dev al lado, asi que esta es la otra
    return _entorno_de_url(env.get(key, ""))


def _entorno_de_url(url: str) -> str:
    host = _host(url)
    return "dev" if host in ("localhost", "127.0.0.1", "::1") else "?"


def _host(url: str) -> str:
    m = re.search(r"@([^:/?]+)", url)
    return m.group(1) if m else "?"


def _avisar_base(url: str, source: str, entorno: str) -> None:
    """Por stderr, asi no ensucia el --json ni la tabla pero el que lee siempre lo ve."""
    marca = {"PRODUCCION": "!! PRODUCCION", "dev": "dev"}.get(entorno, "entorno desconocido")
    print(f"db: {_host(url)} [{marca}] (de {source})", file=sys.stderr)


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
    url, source, entorno = resolve_url(a)
    write = getattr(a, "write", False)
    if write and entorno != "dev" and not getattr(a, "prod", False):
        core.die(
            f"--write contra {_host(url)} (de {source}), que no es una base de dev "
            f"({entorno}). Si es a proposito, agregá --prod."
        )
    if not getattr(a, "_base_avisada", False):
        _avisar_base(url, source, entorno)
        a._base_avisada = True
    conn = psycopg2.connect(fix_ssl(url), connect_timeout=10)
    conn.set_session(readonly=not write, autocommit=True)
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
    limitado = bool(a.limit and re.match(r"^\s*select\b", sql, re.I) and " limit " not in sql.lower())
    if limitado:
        sql = f"{sql.rstrip().rstrip(';')} LIMIT {a.limit}"
    rows, note = run_sql(a, sql)
    # El LIMIT implicito corta en silencio: 50 filas de un SELECT que tiene 1500 parecen el
    # resultado entero, y cualquier conteo o chequeo "sobre todo" hecho con eso es falso.
    truncated = limitado and len(rows) >= a.limit
    if truncated:
        print(
            f"ojo: se corto en {a.limit} filas (LIMIT implicito); puede haber mas. "
            "--limit 0 para traer todas.",
            file=sys.stderr,
        )
    core.out(
        {"rows": rows, "count": len(rows), "truncated": truncated, "status": note},
        a.json,
        core.table(rows) + f"\n({note})",
    )


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
    url, source, entorno = resolve_url(a)
    info = {**rows[0], "url_from": source, "host": _host(url), "entorno": entorno}
    core.out(
        info,
        a.json,
        f"ok: {info['db']} como {info['user']} en {info['host']} [{entorno}] (URL de {source})"
        f"\n{info['version'][:80]}",
    )


def register(sub):
    p = sub.add_parser(
        "db", help="consultar la base PostgreSQL del proyecto (dev y solo lectura por defecto)"
    )
    p.add_argument("--url", help="connection string explicita")
    p.add_argument("--key", help="variable de entorno de donde sacar la URL")
    p.add_argument("--schema", default="public")
    p.add_argument("--write", action="store_true", help="permitir sentencias que escriben")
    p.add_argument(
        "--prod",
        action="store_true",
        help="usar la base de produccion aunque el proyecto tenga una de dev (y permitir --write ahi)",
    )
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

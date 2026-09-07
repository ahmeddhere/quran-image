#!/usr/bin/env python3
"""Build ``data/layout.sqlite`` from the legacy MySQL dump.

This replaces the MySQL dependency of the old Perl pipeline.  The Perl code
(``lib/Quran/DB.pm``) read four tables to lay out a page:

    glyph_type        - names of glyph classes (word / end / sura / ...)
    glyph             - font_file + codepoint + page for every glyph
    glyph_page_line   - every glyph placed on a page (page, line, position, type)
    glyph_ayah        - maps glyphs to (sura, ayah, position)  [used for bboxes]

We parse the ``INSERT INTO`` statements out of ``sql/02-database.sql`` (a
``mysqldump`` file), apply the ``sql/03-basmallah-shaddah.sql`` patch, and
write a small SQLite database with the same columns and the indexes the
render pipeline needs.

Usage::

    python scripts/import_layout.py \
        --sql-dir ../quran.com-images-master/sql \
        --out data/layout.sqlite
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
DEFAULT_SQL_DIR = os.path.normpath(
    os.path.join(PROJECT_ROOT, "..", "quran.com-images-master", "sql")
)
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "data", "layout.sqlite")

WANTED_TABLES = ("glyph_type", "glyph", "glyph_page_line", "glyph_ayah")

# Column order as emitted by mysqldump for this dump (verified against
# sql/01-schema.sql / the CREATE TABLE statements in 02-database.sql).
COLUMNS = {
    "glyph_type": ["glyph_type_id", "name", "description", "parent_id"],
    "glyph": [
        "glyph_id",
        "font_file",
        "glyph_code",
        "page_number",
        "glyph_type_id",
        "glyph_type_meta",
        "description",
    ],
    "glyph_page_line": [
        "glyph_page_line_id",
        "glyph_id",
        "page_number",
        "line_number",
        "position",
        "line_type",
    ],
    "glyph_ayah": [
        "glyph_ayah_id",
        "glyph_id",
        "sura_number",
        "ayah_number",
        "position",
    ],
}

SCHEMA = """
CREATE TABLE glyph_type (
    glyph_type_id   INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    parent_id       INTEGER
);
CREATE TABLE glyph (
    glyph_id        INTEGER PRIMARY KEY,
    font_file       TEXT NOT NULL,
    glyph_code      INTEGER NOT NULL,
    page_number     INTEGER NOT NULL,
    glyph_type_id   INTEGER,
    glyph_type_meta INTEGER,
    description     TEXT
);
CREATE TABLE glyph_page_line (
    glyph_page_line_id INTEGER PRIMARY KEY,
    glyph_id           INTEGER NOT NULL,
    page_number        INTEGER NOT NULL,
    line_number        INTEGER NOT NULL,
    position           INTEGER NOT NULL,
    line_type          TEXT
);
CREATE TABLE glyph_ayah (
    glyph_ayah_id  INTEGER PRIMARY KEY,
    glyph_id       INTEGER NOT NULL,
    sura_number    INTEGER NOT NULL,
    ayah_number    INTEGER NOT NULL,
    position       INTEGER NOT NULL
);
CREATE INDEX ix_gpl_page ON glyph_page_line(page_number, line_number, position);
CREATE INDEX ix_glyph_type_name ON glyph_type(name);
CREATE INDEX ix_glyph_ayah_glyph ON glyph_ayah(glyph_id);
"""


# --------------------------------------------------------------------------- #
# mysqldump INSERT parsing
# --------------------------------------------------------------------------- #
_INSERT_RE = re.compile(
    r"INSERT INTO `(?P<table>\w+)` VALUES\s*(?P<body>.*?);\s*\n", re.DOTALL
)


def _split_rows(body: str):
    """Yield the raw text of each ``( ... )`` tuple in a VALUES body.

    A tiny state machine so single-quoted strings containing ``(``, ``)`` or
    ``,`` (and the MySQL ``''`` / ``\\'`` quote escapes) are handled safely.
    """
    i, n = 0, len(body)
    while i < n:
        while i < n and body[i] != "(":
            i += 1
        if i >= n:
            return
        i += 1  # skip '('
        start = i
        in_str = False
        while i < n:
            c = body[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == "'":
                    if i + 1 < n and body[i + 1] == "'":  # '' escape
                        i += 2
                        continue
                    in_str = False
                    i += 1
                    continue
                i += 1
                continue
            if c == "'":
                in_str = True
                i += 1
                continue
            if c == ")":
                yield body[start:i]
                i += 1
                break
            i += 1


def _split_values(row: str):
    """Split one tuple's text into raw field tokens (still quoted/typed)."""
    fields = []
    i, n = 0, len(row)
    buf = []
    in_str = False
    while i < n:
        c = row[i]
        if in_str:
            buf.append(c)
            if c == "\\":
                if i + 1 < n:
                    buf.append(row[i + 1])
                    i += 2
                    continue
            elif c == "'":
                if i + 1 < n and row[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            buf.append(c)
            i += 1
            continue
        if c == ",":
            fields.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    fields.append("".join(buf).strip())
    return fields


def _coerce(token: str):
    if token == "NULL":
        return None
    if token.startswith("'") and token.endswith("'"):
        s = token[1:-1].replace("''", "'")
        s = re.sub(r"\\(.)", r"\1", s)  # unescape \' \" \\ etc.
        return s
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    try:
        return float(token)
    except ValueError:
        return token


def parse_dump(sql_text: str):
    """Return ``{table: [tuple, ...]}`` for the wanted tables."""
    out = {t: [] for t in WANTED_TABLES}
    for m in _INSERT_RE.finditer(sql_text):
        table = m.group("table")
        if table not in out:
            continue
        ncols = len(COLUMNS[table])
        for raw in _split_rows(m.group("body")):
            vals = [_coerce(tok) for tok in _split_values(raw)]
            if len(vals) != ncols:
                raise ValueError(
                    f"{table}: expected {ncols} columns, got {len(vals)}: {raw!r}"
                )
            out[table].append(tuple(vals))
    return out


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build(sql_dir: str, out_path: str) -> None:
    db_sql = os.path.join(sql_dir, "02-database.sql")
    patch_sql = os.path.join(sql_dir, "03-basmallah-shaddah.sql")
    if not os.path.isfile(db_sql):
        sys.exit(f"error: {db_sql} not found (pass --sql-dir)")

    t0 = time.time()
    print(f"reading {db_sql} ...")
    with open(db_sql, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    print("parsing INSERT statements ...")
    data = parse_dump(text)
    for tbl in WANTED_TABLES:
        print(f"  {tbl:16s} {len(data[tbl]):>8d} rows")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)
    con = sqlite3.connect(out_path)
    try:
        con.executescript(SCHEMA)
        for tbl in WANTED_TABLES:
            ph = ",".join("?" * len(COLUMNS[tbl]))
            con.executemany(f"INSERT INTO {tbl} VALUES ({ph})", data[tbl])
        con.commit()

        # --- apply the basmalah-shaddah patch (sql/03-basmallah-shaddah.sql) ---
        patched = 0
        if os.path.isfile(patch_sql):
            with open(patch_sql, "r", encoding="utf-8") as fh:
                for stmt in fh.read().split(";"):
                    stmt = " ".join(
                        ln for ln in stmt.splitlines() if not ln.strip().startswith("--")
                    ).strip()
                    if stmt:
                        con.execute(stmt)
                        patched += con.total_changes
            con.commit()
        patched = con.execute(
            "SELECT COUNT(*) FROM glyph_page_line "
            "WHERE glyph_page_line_id IN (87950, 88094) AND glyph_id = 4"
        ).fetchone()[0]
        print(f"applied basmalah-shaddah patch ({patched}/2 target rows now glyph_id=4)")

        _verify(con)
    finally:
        con.close()
    print(f"wrote {out_path}  ({os.path.getsize(out_path)/1e6:.1f} MB, {time.time()-t0:.1f}s)")


def _verify(con: sqlite3.Connection) -> None:
    def one(q):
        return con.execute(q).fetchone()[0]

    checks = {
        "glyph rows": (one("SELECT COUNT(*) FROM glyph"), 98139),
        "glyph_page_line rows": (one("SELECT COUNT(*) FROM glyph_page_line"), 88811),
        "glyph_type rows": (one("SELECT COUNT(*) FROM glyph_type"), 16),
        "glyph_ayah rows": (one("SELECT COUNT(*) FROM glyph_ayah"), 88246),
        "min page": (one("SELECT MIN(page_number) FROM glyph_page_line"), 1),
        "max page": (one("SELECT MAX(page_number) FROM glyph_page_line"), 604),
        "distinct pages": (
            one("SELECT COUNT(DISTINCT page_number) FROM glyph_page_line"),
            604,
        ),
    }
    bad = []
    for label, (got, want) in checks.items():
        flag = "ok" if got == want else "MISMATCH"
        if got != want:
            bad.append(label)
        print(f"  check {label:24s} = {got:<8} (expect {want})  {flag}")

    lines_per_page = con.execute(
        """SELECT n, COUNT(*) FROM (
               SELECT page_number, COUNT(DISTINCT line_number) n
               FROM glyph_page_line GROUP BY page_number
           ) GROUP BY n ORDER BY n"""
    ).fetchall()
    print(f"  lines-per-page distribution: {dict(lines_per_page)}")
    if dict(lines_per_page) != {8: 2, 15: 602}:
        bad.append("lines-per-page distribution")

    missing = [
        p
        for (p,) in con.execute(
            "WITH RECURSIVE s(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM s WHERE x<604)"
            " SELECT x FROM s WHERE x NOT IN (SELECT DISTINCT page_number FROM glyph_page_line)"
        )
    ]
    if missing:
        bad.append(f"missing pages {missing}")
        print(f"  MISSING PAGES: {missing}")

    if bad:
        sys.exit("verification FAILED: " + "; ".join(bad))
    print("verification: all checks passed")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sql-dir", default=DEFAULT_SQL_DIR, help="dir with 02-database.sql / 03-basmallah-shaddah.sql")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output sqlite path")
    args = ap.parse_args(argv)
    build(args.sql_dir, args.out)


if __name__ == "__main__":
    main()

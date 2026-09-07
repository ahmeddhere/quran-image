"""Read page/line/glyph layout data from ``data/layout.sqlite``.

Mirrors the two queries the Perl code used (``lib/Quran/DB.pm``):

* ``get_page_lines(page)`` - every glyph on a page, grouped into lines,
  ordered ``line_number ASC, position DESC`` (i.e. left-to-right).
* ``get_ornament_glyph(name)`` - look up an ornament glyph by type name
  (the renderer only needs ``header-box``).

No MySQL, no ``DBI`` - just the standard-library ``sqlite3`` module.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)


def _has_ttf(path: str) -> bool:
    try:
        return any(f.lower().endswith(".ttf") for f in os.listdir(path))
    except OSError:
        return False


# Fonts resolve from  $QURAN_FONTS_DIR  ->  <repo>/assets/fonts  ->  <repo>/fonts
# ->  the legacy ../quran.com-images-master/res/fonts.  The DB path can be
# overridden with $QURAN_DB.
_FONT_CANDIDATES = (
    os.path.join(_PROJECT_ROOT, "assets", "fonts"),
    os.path.join(_PROJECT_ROOT, "fonts"),
    os.path.normpath(
        os.path.join(_PROJECT_ROOT, "..", "quran.com-images-master", "res", "fonts")
    ),
)

DEFAULT_DB = os.environ.get(
    "QURAN_DB", os.path.join(_PROJECT_ROOT, "data", "layout.sqlite")
)
DEFAULT_FONTS = (
    os.environ.get("QURAN_FONTS_DIR")
    or os.environ.get("QURAN_FONTS")
    or next((c for c in _FONT_CANDIDATES if _has_ttf(c)), _FONT_CANDIDATES[-1])
)

# Perl: my $glyph_text = '&#'. $glyph_code .';';  (an HTML entity that GD decodes
# to a single codepoint).  We skip the string form and carry the codepoint.


@dataclass
class Glyph:
    page_line_id: int
    code: int          # codepoint
    type: str | None   # glyph_type.name  (word / end / pause / sura / ...)
    position: int
    # ayah membership (``glyph_ayah``); ``None`` on sura / bismillah lines.
    sura: int | None = None
    ayah: int | None = None
    ayah_pos: int | None = None  # 1-based glyph index within the ayah (all glyph types)


@dataclass
class Line:
    number: int
    type: str | None   # 'sura' | 'ayah' | 'bismillah'
    font: str          # absolute path to the .TTF for this line
    glyphs: list[Glyph] = field(default_factory=list)

    @property
    def codes(self) -> list[int]:
        return [g.code for g in self.glyphs]


class LayoutDB:
    def __init__(self, db_path: str = DEFAULT_DB, fonts_dir: str = DEFAULT_FONTS):
        if not os.path.isfile(db_path):
            raise FileNotFoundError(
                f"{db_path} missing - run scripts/import_layout.py first"
            )
        self.fonts_dir = fonts_dir
        self._con = sqlite3.connect(db_path)
        self._con.row_factory = sqlite3.Row

    # -- font path helper --------------------------------------------------- #
    def font_path(self, font_file: str) -> str:
        return os.path.join(self.fonts_dir, font_file)

    @property
    def bsml_path(self) -> str:
        return self.font_path("QCF_BSML.TTF")

    # -- queries ---------------------------------------------------------------
    def get_page_lines(self, page: int) -> list[Line]:
        rows = self._con.execute(
            """
            SELECT gpl.glyph_page_line_id AS gpl_id,
                   gpl.line_number        AS line_number,
                   gpl.line_type          AS line_type,
                   gpl.position           AS position,
                   g.font_file            AS font_file,
                   g.glyph_code           AS glyph_code,
                   gt.name                AS glyph_type,
                   ga.sura_number         AS sura_number,
                   ga.ayah_number         AS ayah_number,
                   ga.position            AS ayah_position
            FROM glyph_page_line gpl
            LEFT JOIN glyph g       ON g.glyph_id = gpl.glyph_id
            LEFT JOIN glyph_type gt ON g.glyph_type_id = gt.glyph_type_id
            LEFT JOIN glyph_ayah ga ON ga.glyph_id = gpl.glyph_id
            WHERE gpl.page_number = ?
            ORDER BY gpl.line_number ASC, gpl.position DESC
            """,
            (page,),
        ).fetchall()

        lines: dict[int, Line] = {}
        order: list[int] = []
        for r in rows:
            ln = r["line_number"]
            if ln not in lines:
                lines[ln] = Line(
                    number=ln,
                    type=r["line_type"],
                    font=self.font_path(r["font_file"]),
                )
                order.append(ln)
            lines[ln].glyphs.append(
                Glyph(
                    page_line_id=r["gpl_id"],
                    code=r["glyph_code"],
                    type=r["glyph_type"],
                    position=r["position"],
                    sura=r["sura_number"],
                    ayah=r["ayah_number"],
                    ayah_pos=r["ayah_position"],
                )
            )
        return [lines[ln] for ln in order]

    def word_index_map(self, page: int) -> dict[tuple[int, int, int], int]:
        """``(sura, ayah, glyph_ayah.position) -> word index`` for every ayah
        that places a glyph on ``page``.

        The index is 1-based over *word* glyphs in recitation order; the
        ayah-number marker and the pause / sajdah / hizb glyphs all map to
        ``0`` (the device treats ``0`` as "the ayah's number roundel").  It is
        computed from each ayah's **full** glyph run - which may begin on an
        earlier page - so a verse split across a page break keeps one
        continuous numbering, and it lines up with
        ``QuranMetadataRepository.getAyahWords`` on the client, which counts
        the same word glyphs and skips the same marks.
        """
        rows = self._con.execute(
            """
            SELECT ga.sura_number AS sura,
                   ga.ayah_number AS ayah,
                   ga.position    AS pos,
                   gt.name        AS gtype
            FROM glyph_ayah ga
            JOIN glyph g            ON g.glyph_id = ga.glyph_id
            LEFT JOIN glyph_type gt ON gt.glyph_type_id = g.glyph_type_id
            JOIN (
                SELECT DISTINCT ga2.sura_number AS s, ga2.ayah_number AS a
                FROM glyph_page_line gpl
                JOIN glyph_ayah ga2 ON ga2.glyph_id = gpl.glyph_id
                WHERE gpl.page_number = ?
            ) pa ON pa.s = ga.sura_number AND pa.a = ga.ayah_number
            ORDER BY ga.sura_number, ga.ayah_number, ga.position
            """,
            (page,),
        ).fetchall()

        out: dict[tuple[int, int, int], int] = {}
        current: tuple[int, int] | None = None
        word_no = 0
        for r in rows:
            key = (r["sura"], r["ayah"])
            if key != current:
                current, word_no = key, 0
            if r["gtype"] == "word":
                word_no += 1
                out[(r["sura"], r["ayah"], r["pos"])] = word_no
            else:
                out[(r["sura"], r["ayah"], r["pos"])] = 0
        return out

    def get_ornament_glyph(self, name: str) -> Glyph:
        r = self._con.execute(
            """
            SELECT g.glyph_code AS code, gt.name AS name
            FROM glyph g
            JOIN glyph_type gt  ON g.glyph_type_id = gt.glyph_type_id
            JOIN glyph_type gtp ON gt.parent_id = gtp.glyph_type_id
            WHERE gtp.name = 'ornament' AND gt.name = ?
            """,
            (name,),
        ).fetchone()
        if r is None:
            raise KeyError(f"ornament glyph {name!r} not found")
        return Glyph(page_line_id=0, code=r["code"], type=r["name"], position=0)

    def close(self) -> None:
        self._con.close()

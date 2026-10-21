"""corpus-snapshot + corpus-census (issue #177, 4.0 program #176).

The parser is tested against the REAL renderer (corpus._render_entry) so a
render change that breaks the text transport turns this red.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, ErrorRow, KnowledgeRow, NegativeRow, RuleRow
from mcm_engine.corpus_snapshot import census, parse_scroll_page, snapshot
from mcm_engine.tools.corpus import _TEXT_FIELDS, _render_entry

BODY = "para one\n\npara two with a blank line above\n  indented line\nlast"


def _seed(storage: SqliteStorage) -> None:
    storage.insert_knowledge(KnowledgeRow(
        id=0, topic="Widget cache", summary="LWW per name", detail=BODY,
        tags="cache,policy", rationale="because"))
    storage.insert_negative(NegativeRow(
        id=0, category="measurement", what_failed="what\nfailed over\n\ntwo paras",
        why_failed="why " + BODY, correct_approach="fix"))
    storage.insert_error(ErrorRow(
        id=0, pattern="multi\nline pattern headline", context="ctx",
        root_cause="rc", fix=BODY, tags="t1,t2"))
    storage.insert_rule(RuleRow(
        id=0, title="Rule title", keywords="k1,k2", description="desc",
        content="# body\n\n" + BODY, category="conventions"))


def _render_page(storage: SqliteStorage, et: EntityType) -> str:
    rows = storage.page_entries(et, after_id=0, limit=100)
    blocks = [_render_entry(et, r, _TEXT_FIELDS[et]) for r in rows]
    return "\n\n".join(blocks) + f"\n\n--- page: {len(rows)} {et.value} entries (cap 100). next: x"


@pytest.fixture
def source(tmp_path):
    s = SqliteStorage(db_path=str(tmp_path / "src.db"))
    s.ensure_schema()
    _seed(s)
    return s


@pytest.mark.parametrize("et", list(EntityType))
def test_parse_round_trips_the_real_renderer(source, et):
    text = _render_page(source, et)
    parsed = parse_scroll_page(text)
    rows = {r.id: r for r in source.iter_entries(et)}
    assert [p.id for p in parsed] == sorted(rows)
    for p in parsed:
        row = rows[p.id]
        for f in _TEXT_FIELDS[et]:
            expected = getattr(row, f, None)
            if expected:
                assert p.fields.get(f) == expected, f"{et.value}.{f}"


def test_parse_handles_empty_page():
    assert parse_scroll_page("No knowledge entries with id > 7. End of corpus for this type.") == []


def test_snapshot_copies_every_type_with_ids(source, tmp_path):
    def call(name, args):
        assert name == "scroll_entries"
        et = EntityType(args["entity_type"])
        rows = source.page_entries(et, after_id=args["after_id"], limit=args["limit"])
        if not rows:
            return f"No {et.value} entries with id > {args['after_id']}. End of corpus."
        blocks = [_render_entry(et, r, _TEXT_FIELDS[et]) for r in rows]
        return "\n\n".join(blocks) + "\n\n--- page: n"

    out = tmp_path / "snap.db"
    report = snapshot(call, out, page=2)
    assert report.counts == {"knowledge": 1, "negative": 1, "error": 1, "rule": 1}

    snap = SqliteStorage(db_path=str(out))
    for et in EntityType:
        src_rows = {r.id: r for r in source.iter_entries(et)}
        snap_rows = {r.id: r for r in snap.iter_entries(et)}
        assert set(src_rows) == set(snap_rows)
        for rid, r in src_rows.items():
            for f in _TEXT_FIELDS[et]:
                assert getattr(snap_rows[rid], f, None) == getattr(r, f, None), f"{et.value}.{f}"


def test_snapshot_refuses_to_overwrite(tmp_path):
    p = tmp_path / "exists.db"
    p.write_text("x")
    with pytest.raises(FileExistsError):
        snapshot(lambda n, a: "", p)


def test_census_reports_shape_pollution_and_fit(source):
    # one polluted NEGATIVE (serialized blob in the headline column) and one
    # unfit ERROR (title only, nothing else) alongside the clean seed rows
    source.insert_negative(NegativeRow(
        id=0, category="c",
        what_failed='real</what_failed>\n<parameter name="why_failed">w</parameter></invoke>'))
    source.insert_error(ErrorRow(id=0, pattern="bare pattern"))

    rep = census(source)
    neg = rep["tables"]["negative"]
    err = rep["tables"]["error"]
    assert neg["rows"] == 2 and neg["polluted"]["count"] == 1
    assert neg["unfit"]["count"] == 1 and neg["unfit"]["ids"] == neg["polluted"]["ids"]
    assert err["rows"] == 2 and err["unfit"]["count"] == 1
    k = rep["tables"]["knowledge"]["columns"]
    assert k["detail"]["null_rate"] == 0.0 and k["detail"]["len_max"] == len(BODY)
    assert k["alternatives"]["null_rate"] == 1.0
    assert rep["total_rows"] == 6 and rep["total_unfit"] == 2

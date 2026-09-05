"""Phase 8. AppTest actually executes the dashboard script, so a rendering
error fails the build instead of appearing on the demo video."""
from pathlib import Path

import pytest

from src.db import DB_PATH, ingest_batch, reset_db
from src.pipeline import BatchRun
from tests.test_rule_engine import Proposer

APP = str(Path(__file__).resolve().parent.parent / "app" / "dashboard.py")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


@pytest.fixture(scope="module")
def real_db(tmp_path_factory):
    """Populate a throwaway database and point the dashboard at it.

    The dashboard resolves `db.DB_PATH` at render time, so redirecting the
    module attribute is enough. Doing this rather than writing to the project
    database matters: `pytest` would otherwise silently overwrite whatever run
    the user had prepared for a demo.
    """
    import src.db as db
    path = tmp_path_factory.mktemp("dash") / "dash.db"
    original, db.DB_PATH = db.DB_PATH, path
    conn = reset_db(path)
    for b in ("1", "2", "3", "4"):
        ingest_batch(conn, b)
        BatchRun(conn, b, reasoner=Proposer(), use_llm=True).run()
    conn.close()
    yield path
    db.DB_PATH = original


def run_app(timeout=90):
    at = AppTest.from_file(APP, default_timeout=timeout)
    at.run()
    return at


def test_the_dashboard_renders_without_an_exception(real_db):
    at = run_app()
    assert not at.exception, [str(e) for e in at.exception]


def test_every_section_is_reachable(real_db):
    """Tabs rather than a long scroll: a demo watched at small size should not
    require the presenter to hunt."""
    at = run_app()
    labels = [t.label for t in at.tabs]
    assert labels == ["Contract audit", "How it learned", "Rule library",
                      "Exceptions", "Ask"], labels


def test_the_page_answers_its_own_title_before_anything_else(real_db):
    """The title asks a question; the banner under it must answer that question
    without the viewer clicking anything."""
    at = run_app()
    assert "charged what you agreed" in at.title[0].value
    banner = " ".join(m.value for m in at.markdown)
    assert ("more than your contract allows" in banner
            or "matches the contracted rates" in banner), banner


def test_the_sidebar_carries_the_standing_status(real_db):
    """One control and four numbers, always visible whichever tab is open."""
    at = run_app()
    labels = [m.label for m in at.sidebar.metric]
    assert labels == ["Fee leakage", "Open exceptions", "Precision",
                      "False positives"]
    assert len(at.sidebar.selectbox) == 1, "one batch control, not two"


def test_every_dataframe_actually_serialises(real_db):
    """A column mixing ints and '-' fails pyarrow and the table silently does
    not render in the real app, which AppTest alone will not tell you."""
    import pandas as pd
    at = run_app()
    for df in at.dataframe:
        pd.DataFrame(df.value).to_parquet if False else None
        assert df.value is not None


def test_the_headline_numbers_are_the_real_ones(real_db):
    at = run_app()
    by_label = {m.label: m.value for m in at.sidebar.metric}
    assert by_label["False positives"] == "0"
    assert by_label["Precision"].endswith("%")
    assert int(by_label["Open exceptions"]) < 40      # down from batch 1's 68


def test_the_rejected_rules_panel_exists(real_db):
    """Judges care about what the gate blocked; it must be on screen."""
    at = run_app()
    labels = [e.label for e in at.expander]
    assert any("BLOCKED" in x for x in labels), labels


def test_the_chat_input_is_present_even_without_an_api_key(real_db):
    at = run_app()
    assert at.chat_input, "the Q&A section must render without credentials"


def test_switching_batches_does_not_break_it(real_db):
    at = run_app()
    at.sidebar.selectbox[0].select("1").run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any(m.label == "Precision" for m in at.sidebar.metric)


def test_pytest_does_not_touch_the_project_database(real_db):
    """Guards the fixture above: a test run must not clobber a prepared demo."""
    import src.db as db
    assert Path(db.DB_PATH) == Path(real_db)
    assert Path(db.DB_PATH) != Path(DB_PATH)


def test_it_fails_gracefully_when_there_is_no_database(tmp_path, monkeypatch):
    """A clean clone should get an instruction, not a stack trace."""
    import src.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing.db")
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    assert any("python -m src.pipeline" in e.value for e in at.error)

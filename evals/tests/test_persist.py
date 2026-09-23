"""Persistence of a finished report into the application database."""

from __future__ import annotations

from sqlalchemy import create_engine, select

from evals.persist import classify_value, normalise_url, persist_report


def test_classify_value():
    assert classify_value(None) == ("null", None, None)
    assert classify_value(True) == ("boolean", 1.0, "true")
    assert classify_value(False) == ("boolean", 0.0, "false")
    assert classify_value(7) == ("number", 7.0, None)
    assert classify_value(2.5) == ("number", 2.5, None)
    assert classify_value("5/5") == ("text", None, "5/5")


def test_normalise_url_upgrades_postgres_schemes():
    assert normalise_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("sqlite+pysqlite:///x.db") == "sqlite+pysqlite:///x.db"


def test_persist_report_writes_rows(tmp_path, report):
    from sqlalchemy.orm import sessionmaker

    from app.models import Base, EvaluationMetric, EvaluationReport, EvaluationSuite

    db = tmp_path / "evals.db"
    url = f"sqlite+pysqlite:///{db}"

    setup = create_engine(url, future=True)
    Base.metadata.create_all(setup)
    setup.dispose()

    report_id = persist_report(report, url)

    engine = create_engine(url, future=True)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        reports = session.execute(select(EvaluationReport)).scalars().all()
        assert len(reports) == 1
        row = reports[0]
        assert row.id == report_id
        assert row.status == "failed"
        assert row.suites_total == 2
        assert row.suites_passed == 1
        assert row.cases_total == 3
        assert row.cases_passed == 2
        assert row.report_json["status"] == "failed"

        suites = session.execute(select(EvaluationSuite)).scalars().all()
        assert {s.suite_id for s in suites} == {"ranking", "grounding"}
        failed = next(s for s in suites if s.suite_id == "grounding")
        assert failed.error == "a grounding failure"

        metrics = session.execute(select(EvaluationMetric)).scalars().all()
        assert len(metrics) == 6  # 5 on ranking + 1 on grounding
        violation = next(m for m in metrics if m.label == "violations")
        assert violation.value_type == "number"
        assert violation.value_num == 2.0
        not_measured = next(m for m in metrics if m.label == "pricing")
        assert not_measured.value_type == "null"
        assert not_measured.value_num is None
        boolean = next(m for m in metrics if m.label == "enabled")
        assert boolean.value_type == "boolean"
        assert boolean.value_num == 1.0
    engine.dispose()

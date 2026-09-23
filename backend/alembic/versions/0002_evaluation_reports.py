"""evaluation reports, suites and metrics

Revision ID: 0002_evaluation_reports
Revises: 0001_initial_schema
Create Date: 2026-09-23 12:55:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_evaluation_reports"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("git_commit", sa.String(length=40), nullable=True),
        sa.Column("git_dirty", sa.Boolean(), nullable=False),
        sa.Column("methodology_version", sa.String(length=16), nullable=False),
        sa.Column("suites_total", sa.Integer(), nullable=False),
        sa.Column("suites_passed", sa.Integer(), nullable=False),
        sa.Column("cases_total", sa.Integer(), nullable=False),
        sa.Column("cases_passed", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eval_reports_generated_at", "evaluation_reports", ["generated_at"], unique=False
    )

    op.create_table(
        "evaluation_suites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("suite_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("cases_total", sa.Integer(), nullable=False),
        sa.Column("cases_passed", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["evaluation_reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", "suite_id", name="uq_eval_suite_report"),
    )
    op.create_index("ix_eval_suites_report", "evaluation_suites", ["report_id"], unique=False)

    op.create_table(
        "evaluation_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("suite_id", sa.String(length=64), nullable=False),
        sa.Column("suite_title", sa.String(length=200), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["evaluation_reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eval_metrics_report_suite", "evaluation_metrics", ["report_id", "suite_id"], unique=False
    )
    op.create_index("ix_eval_metrics_label", "evaluation_metrics", ["label"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_eval_metrics_label", table_name="evaluation_metrics")
    op.drop_index("ix_eval_metrics_report_suite", table_name="evaluation_metrics")
    op.drop_table("evaluation_metrics")
    op.drop_index("ix_eval_suites_report", table_name="evaluation_suites")
    op.drop_table("evaluation_suites")
    op.drop_index("ix_eval_reports_generated_at", table_name="evaluation_reports")
    op.drop_table("evaluation_reports")

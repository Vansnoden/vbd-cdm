# path: alembic/versions/b3f19a7c4e05_run_manifest.py
# -*- coding: utf-8 -*-
"""run_manifest: what produced this database, recorded in the database.

Revision ID: b3f19a7c4e05
Revises: 698bad4f2cd5
Create Date: 2026-11-20 12:00:00.000000

Why this table
--------------
The database can say, for every resolution, which vocabulary release produced
it: ``fact_concept_map.vocabulary_version`` and ``dim_vocabulary.checksum``
carry that. It could not say which *expansion policy* produced the modelled case
rows, nor which dependency versions produced the database at all.

``run_manifest`` closes that gap with one row per pipeline invocation. It is not
append-only in the same sense as ``fact_resolution_log``: a run legitimately
learns about itself as it proceeds, so the row is upserted and later phases merge
their parameters into it. ``fact_resolution_log`` remains the immutable record of
decisions; this table is the record of the run that made them.

Why JSON text rather than columns
---------------------------------
``parameters`` and ``row_counts`` hold whatever the phases of the day consider
provenance. Adding a key must not require a migration, and a column-per-key
design would either freeze the schema or accumulate nullable columns nobody
reads. JSON text keeps the schema stable and the payload explicit.

Why no foreign keys
-------------------
A manifest must survive the database it describes being partially rebuilt: a
researcher who drops and re-ingests ``fact_disease_case`` has not thereby
invalidated the record of the run that first populated it.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b3f19a7c4e05"
down_revision: Union[str, Sequence[str], None] = "698bad4f2cd5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create ``run_manifest``."""
    op.create_table(
        "run_manifest",
        sa.Column("run_id", sa.String(length=50), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        # NULL until the run finishes: an interrupted run is identifiable as
        # interrupted, rather than indistinguishable from a completed one.
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("tool_version", sa.String(length=20), nullable=True),
        # Includes a '-dirty' suffix when the tree had uncommitted work, because
        # a bare sha would imply a run is reproducible from that commit.
        sa.Column("git_sha", sa.String(length=64), nullable=True),
        # Two digests, deliberately: digest() is comparable across releases,
        # digest_with_kind() only within one. They are different namespaces.
        sa.Column("registry_digest", sa.String(length=64), nullable=True),
        sa.Column("registry_digest_with_kind", sa.String(length=64), nullable=True),
        sa.Column("countries", sa.Text(), nullable=True),        # JSON list
        sa.Column("config_path", sa.String(length=1000), nullable=True),
        sa.Column("config_sha256", sa.String(length=64), nullable=True),
        sa.Column("parameters", sa.Text(), nullable=True),       # JSON object
        sa.Column("package_versions", sa.Text(), nullable=True), # JSON object
        sa.Column("row_counts", sa.Text(), nullable=True),       # JSON object
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("idx_run_manifest_started", "run_manifest",
                    ["started_at"], unique=False)


def downgrade() -> None:
    """Drop ``run_manifest``.

    Downgrade restores the schema, not the provenance: the manifest rows are the
    only record of which policy produced the modelled rows, and dropping the
    table discards that. A database downgraded below this revision can no longer
    state how its ``ESTIMATE`` case rows were generated.
    """
    op.drop_index("idx_run_manifest_started", table_name="run_manifest")
    op.drop_table("run_manifest")
# path: alembic/versions/d5e2a8c71b93_dim_vocabulary_extra.py
# -*- coding: utf-8 -*-
"""dim_vocabulary.extra: the checksum artefact discriminator.

Revision ID: d5e2a8c71b93
Revises: c41f7b9e2d10
Create Date: 2026-11-19 10:20:00.000000

Why this column has to exist
----------------------------
``dim_vocabulary.checksum`` holds *two different hashes*. The registry writes
the SHA-256 of a pack's **manifest** bytes; ``VocabularyLoader.load_csv`` writes
the SHA-256 of a vocabulary **CSV**. Both are 64 hex characters, so a reader
cannot tell which artefact a stored digest describes — and a digest whose
artefact is unknown cannot answer the question it was stored to answer ("which
knowledge produced this decision").

The contract (Amendment §5.4) puts the discriminator in ``dim_vocabulary.extra``
as ``{"checksum_artefact": "manifest" | "csv"}``. Both writers already emit it
and probe for the column
(``utilities.vocabulary.loader.dim_vocabulary_supports_extra``), logging a
warning and continuing when it is absent. Until this migration runs, on every
database built from the batch-1..6 bundles, that probe returns False and every
checksum written is ambiguous.

Backfill: deliberately none
---------------------------
A pre-existing row's checksum could have come from either writer, and the two
sets overlap (both may have written the same pack). Marking them ``'manifest'``
would be a guess recorded as a fact in the column whose whole purpose is to
avoid exactly that. Rows written before this revision keep ``extra = NULL``,
which reads as "artefact unknown" — the honest answer — and the next run of
either writer fills it in, because both paths are ``ON CONFLICT DO UPDATE``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d5e2a8c71b93"
down_revision: Union[str, Sequence[str], None] = "c41f7b9e2d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``dim_vocabulary.extra``."""
    op.add_column(
        "dim_vocabulary",
        sa.Column("extra", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove ``dim_vocabulary.extra``.

    The discriminator values are dropped with the column. Downgrade restores the
    schema, not the pre-migration state: before this revision no artefact was
    recorded at all, so there is nothing to restore.
    """
    with op.batch_alter_table("dim_vocabulary", schema=None) as batch_op:
        batch_op.drop_column("extra")
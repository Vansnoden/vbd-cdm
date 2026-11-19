# path: alembic/versions/c41f7b9e2d10_resolution_ledger.py
# status: MIGRATION
# -*- coding: utf-8 -*-
"""Resolution ledger: decision provenance, pack metadata, append-only log.

Revision ID: c41f7b9e2d10
Revises: 7aa764c87018
Create Date: 2026-11-18 17:05:00.000000

Implements review §5 step 1 (persistence) only. Nothing in this revision
changes how a value is resolved; it creates the storage that lets batches 2-4
record *what* was decided, *by which vocabulary release*, *by which resolver*,
and *when a human last looked at it*.

Shape of the change
-------------------
``fact_concept_map``
    + decision, vocabulary_version, resolver_version, last_run_id,
      reviewed_by, reviewed_at
    is_approved becomes nullable with no default (three-state: NULL = nobody
    has reviewed it, TRUE = a human approved, FALSE = a human rejected).
    ``uq_concept_map_source`` is unchanged, so every write stays a keyed
    upsert and lookup remains O(1).

``dim_vocabulary``
    + pack_id, checksum, released_with. ``version`` is backfilled from the
    three releases that exist today, else ``'unknown'``.

``dim_concept``
    + concept_norm (indexed). Left NULL for existing rows: the normaliser is
    Python, not SQL, so the loader fills it on the next build. The column
    exists now so batch 2 can write it and batch 3 can index-match on it
    without a second migration.

``fact_resolution_log`` (new, append-only)
    One row per (raw value, run). Carries every ``Resolution`` field from
    interface_contract.md plus a surrogate PK and ``resolved_at``.

Backfills are deliberately conservative
---------------------------------------
* ``decision`` is derived from the *old* ``is_approved`` semantics
  (``is_approved = confident``, i.e. the matcher's own confidence), so
  ``True`` -> ``auto_accepted`` and ``False`` -> ``needs_review``. It is never
  ``rejected``: no human ever reviewed those rows, and ``reviewed_by`` /
  ``reviewed_at`` stay NULL to say so.
* ``released_with`` and ``checksum`` stay NULL for pre-registry rows. They are
  unknowable after the fact; inventing ``'1.0.0'`` would be a lie in an audit
  trail.

Downgrade
---------
Drops every added column, index and table, restoring the exact column set of
revision 7aa764c87018. The backfilled values are dropped with the columns, so
downgrade is a schema reversal, not a data restoration — see the note in
``downgrade()``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "c41f7b9e2d10"
down_revision: Union[str, Sequence[str], None] = "7aa764c87018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Vocabulary releases that existed before the registry did. Used only to
#: backfill ``dim_vocabulary.version`` and to give each legacy row the same
#: ``pack_id`` its manifest declares. Both members of each pair are module
#: literals, never user input, which is why they may be interpolated into SQL.
KNOWN_VOCABULARY_RELEASES: tuple[tuple[str, str], ...] = (
    ("GADM", "4.1"),
    ("IDOMAL", "2024-05"),
    ("NCBI", "2024-11"),
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ------------------------------------------------------------------
    # 1. fact_concept_map — decision provenance + human-review state
    # ------------------------------------------------------------------
    op.add_column(
        "fact_concept_map",
        sa.Column("decision", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "fact_concept_map",
        sa.Column("vocabulary_version", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "fact_concept_map",
        sa.Column("resolver_version", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "fact_concept_map",
        sa.Column("last_run_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "fact_concept_map",
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "fact_concept_map",
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
    )

    # is_approved must be nullable with no default: NULL means "nobody has
    # reviewed this", which today's `default=False` conflates with "a human
    # rejected it" (review C4). The initial revision already created the
    # column as nullable, and SQLite cannot ALTER COLUMN without a full table
    # rebuild, so the DDL is emitted only where it is both possible and
    # potentially needed. Skipping the rebuild on SQLite also keeps this
    # migration free of destructive table recreation.
    if dialect != "sqlite":
        op.alter_column(
            "fact_concept_map",
            "is_approved",
            existing_type=sa.Boolean(),
            existing_nullable=True,
            nullable=True,
        )

    # Backfill `decision` from the old single-boolean semantics. The CASE uses
    # bare boolean predicates rather than `= 1` / `= TRUE` so the same
    # statement is valid on SQLite (0/1) and PostgreSQL (true/false).
    op.execute(text("""
        UPDATE fact_concept_map
           SET decision = 'auto_accepted'
         WHERE decision IS NULL
           AND is_approved IS NOT NULL
           AND is_approved
    """))
    op.execute(text("""
        UPDATE fact_concept_map
           SET decision = 'needs_review'
         WHERE decision IS NULL
           AND is_approved IS NOT NULL
           AND NOT is_approved
    """))
    # Rows with is_approved IS NULL keep decision NULL: the matcher never
    # reached them, so neither band applies.

    op.execute(text("""
        UPDATE fact_concept_map
           SET vocabulary_version = 'unknown'
         WHERE vocabulary_version IS NULL
           AND vocabulary_id IS NOT NULL
    """))
    op.execute(text("""
        UPDATE fact_concept_map
           SET resolver_version = 'pre-registry'
         WHERE resolver_version IS NULL
    """))

    op.create_index(
        "idx_concept_map_decision",
        "fact_concept_map",
        ["decision"],
        unique=False,
    )

    # `uq_concept_map_source` is deliberately untouched: the key stays
    # (source_table, source_column, source_value) so writes remain keyed
    # upserts. History lives in fact_resolution_log, not in extra key columns.

    # ------------------------------------------------------------------
    # 2. dim_vocabulary — pack identity, checksum, release lineage
    # ------------------------------------------------------------------
    op.add_column(
        "dim_vocabulary",
        sa.Column("pack_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "dim_vocabulary",
        sa.Column("checksum", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dim_vocabulary",
        sa.Column("released_with", sa.String(length=20), nullable=True),
    )

    # version was always written as NULL by VocabularyLoader.load_csv
    # (review C5). Fill it from the releases that exist today, then mark the
    # rest 'unknown' rather than leaving NULL, so "unknown release" is a
    # value rather than an absence.
    for vocab_id, version in KNOWN_VOCABULARY_RELEASES:
        op.execute(text(f"""
            UPDATE dim_vocabulary
               SET version = '{version}',
                   pack_id = '{vocab_id.lower()}'
             WHERE vocabulary_id = '{vocab_id}'
               AND (version IS NULL OR version = '')
        """))
    op.execute(text("""
        UPDATE dim_vocabulary
           SET version = 'unknown'
         WHERE version IS NULL OR version = ''
    """))
    # pack_id is the manifest key, which is lower-case by convention. Legacy
    # rows get it from their vocabulary_id so the pre-registry row and the
    # registry-loaded row for the same product share a pack_id.
    op.execute(text("""
        UPDATE dim_vocabulary
           SET pack_id = LOWER(vocabulary_id)
         WHERE pack_id IS NULL
    """))
    # released_with and checksum remain NULL for legacy rows: no release ever
    # shipped them, and inventing one would corrupt the audit trail.

    # ------------------------------------------------------------------
    # 3. dim_concept — normalised key, indexed for L2 matching
    # ------------------------------------------------------------------
    op.add_column(
        "dim_concept",
        sa.Column("concept_norm", sa.String(length=300), nullable=True),
    )
    op.create_index(
        "idx_concept_norm",
        "dim_concept",
        ["concept_norm"],
        unique=False,
    )
    # No backfill: the normaliser is Python (utilities.vocabulary.normalizers),
    # and the registry loader populates concept_norm when it next writes the
    # vocabulary. A SQL approximation here would create keys the resolver can
    # never reproduce.

    # ------------------------------------------------------------------
    # 4. fact_resolution_log — append-only decision ledger
    # ------------------------------------------------------------------
    op.create_table(
        "fact_resolution_log",
        sa.Column("log_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_table", sa.String(length=100), nullable=False),
        sa.Column("source_column", sa.String(length=100), nullable=False),
        sa.Column("source_value", sa.String(length=300), nullable=False),
        # No FK to dim_vocabulary on purpose: the ledger must survive a
        # vocabulary row being re-versioned or deleted, and it records
        # resolutions against packs that may not be loaded yet.
        sa.Column("vocabulary_id", sa.String(length=100), nullable=True),
        sa.Column("vocabulary_version", sa.String(length=100), nullable=True),
        sa.Column("concept_code", sa.String(length=100), nullable=True),
        sa.Column("match_method", sa.String(length=30), nullable=True),
        sa.Column("match_score", sa.Float(), nullable=True),
        # NOT NULL: the decision enum has an `unresolved` member, so every
        # logged resolution carries a verdict. Unlike fact_concept_map there
        # are no legacy rows to accommodate.
        sa.Column("decision", sa.String(length=20), nullable=False),
        # JSON: [{vocab, code, name, method, score}, ...] in rank order.
        sa.Column("candidates", sa.Text(), nullable=True),
        sa.Column("resolver_version", sa.String(length=50), nullable=True),
        # In the unique key, therefore NOT NULL: SQL treats NULLs as distinct,
        # which would silently permit duplicate rows for the same run.
        sa.Column("run_id", sa.String(length=50), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("log_id"),
        sa.UniqueConstraint(
            "source_table",
            "source_column",
            "source_value",
            "run_id",
            name="uq_resolution_log_source",
        ),
    )
    op.create_index(
        "idx_resolution_log_decision",
        "fact_resolution_log",
        ["decision"],
        unique=False,
    )
    op.create_index(
        "idx_resolution_log_run",
        "fact_resolution_log",
        ["run_id"],
        unique=False,
    )
    # Append-only is enforced by the writer (no UPDATE/DELETE path exists and
    # the model has no merge path). A DB-level trigger or REVOKE would be
    # dialect-specific and is deliberately out of scope for this batch.


def downgrade() -> None:
    """Downgrade schema.

    Reverses ``upgrade()`` in exact reverse order, restoring the column set of
    revision 7aa764c87018.

    Two deliberate notes:

    * ``fact_concept_map.is_approved`` is *not* altered back to NOT NULL. It
      was already nullable before this revision (see the initial migration),
      so the only truthful reversal is to leave it nullable. A database that
      was NOT NULL before this revision and is downgraded afterwards is left
      nullable — restoring NOT NULL would fail on rows whose review state is
      genuinely unknown.
    * The derived ``decision`` values and the ``dim_vocabulary`` backfills are
      dropped along with the columns that hold them. Downgrade restores the
      schema, not the pre-migration data; the pre-migration state recorded no
      decision at all, so nothing is silently asserted to have been known.
    """
    # 4. fact_resolution_log
    op.drop_index("idx_resolution_log_run", table_name="fact_resolution_log")
    op.drop_index(
        "idx_resolution_log_decision", table_name="fact_resolution_log"
    )
    op.drop_table("fact_resolution_log")

    # 3. dim_concept
    op.drop_index("idx_concept_norm", table_name="dim_concept")
    with op.batch_alter_table("dim_concept", schema=None) as batch_op:
        batch_op.drop_column("concept_norm")

    # 2. dim_vocabulary
    with op.batch_alter_table("dim_vocabulary", schema=None) as batch_op:
        batch_op.drop_column("released_with")
        batch_op.drop_column("checksum")
        batch_op.drop_column("pack_id")

    # 1. fact_concept_map
    op.drop_index("idx_concept_map_decision", table_name="fact_concept_map")
    with op.batch_alter_table("fact_concept_map", schema=None) as batch_op:
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("reviewed_by")
        batch_op.drop_column("last_run_id")
        batch_op.drop_column("resolver_version")
        batch_op.drop_column("vocabulary_version")
        batch_op.drop_column("decision")
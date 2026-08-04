"""Create the complete durable-agent schema.

Revision ID: 0001
Revises: None
Create Date: 2026-08-04

This bootstrapping revision creates the v0.1 metadata. Later migrations use explicit
Alembic operations; release validation prevents modifying v0.1 metadata without adding
a revision.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import MetaData

from durable_agent.persistence.schema_v1 import SchemaV1Base

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all v1 tables and indexes through the migration connection."""
    bind = op.get_bind()
    # This revision intentionally uses a frozen schema snapshot. Importing the live ORM
    # here would silently rewrite migration history whenever application models changed.
    SchemaV1Base.metadata.create_all(bind=bind, checkfirst=False)


def downgrade() -> None:
    """Drop v1 objects in dependency order."""
    bind = op.get_bind()
    metadata = MetaData()
    metadata.reflect(bind=bind)
    metadata.drop_all(bind=bind, checkfirst=False)

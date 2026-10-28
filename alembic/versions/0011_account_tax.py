"""Live tax lots (task 5.3, README §5.7): account_snapshots carries the account's holdings and
the estimated tax a sale of everything would owe at the close (tax_owed; NULL when the lots
couldn't be rebuilt that evening).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: str | Sequence[str] | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('account_snapshots', sa.Column('holdings', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"), nullable=False))
    op.add_column('account_snapshots', sa.Column('tax_owed', sa.Double(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('account_snapshots', 'tax_owed')
    op.drop_column('account_snapshots', 'holdings')

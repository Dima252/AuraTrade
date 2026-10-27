"""The scorecard (task 4.4, README §5.5): twin_book keeps each book's estimated tax at the close
(tax_owed) and a third book, SPY bought and held.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: str | Sequence[str] | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'twin_book', sa.Column('tax_owed', sa.Double(), server_default=sa.text('0'), nullable=False)
    )
    op.drop_constraint(op.f('ck_twin_book_book'), 'twin_book', type_='check')
    op.create_check_constraint(
        op.f('ck_twin_book_book'), 'twin_book', "book IN ('TWIN', 'SIM', 'SPY')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM twin_book WHERE book = 'SPY'")
    op.drop_constraint(op.f('ck_twin_book_book'), 'twin_book', type_='check')
    op.create_check_constraint(op.f('ck_twin_book_book'), 'twin_book', "book IN ('TWIN', 'SIM')")
    op.drop_column('twin_book', 'tax_owed')

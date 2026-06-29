"""add bkt table

Revision ID: 13a74035e2d3
Revises: aa1b2c3d4e5f
Create Date: 2026-03-04 12:14:31.571823

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '13a74035e2d3'
down_revision: Union[str, Sequence[str], None] = 'aa1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'knowledge_mastery',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('knowledge_point_id', sa.String(length=50), nullable=False),
        sa.Column('p_mastery', sa.Float(), nullable=False),
        sa.Column('p_transit', sa.Float(), nullable=False),
        sa.Column('p_guess', sa.Float(), nullable=False),
        sa.Column('p_slip', sa.Float(), nullable=False),
        sa.Column('total_attempts', sa.Integer(), nullable=False),
        sa.Column('correct_attempts', sa.Integer(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'knowledge_point_id', name='uq_user_kp'),
    )
    op.create_index(op.f('ix_knowledge_mastery_user_id'), 'knowledge_mastery', ['user_id'], unique=False)
    op.create_index(op.f('ix_knowledge_mastery_knowledge_point_id'), 'knowledge_mastery', ['knowledge_point_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_knowledge_mastery_knowledge_point_id'), table_name='knowledge_mastery')
    op.drop_index(op.f('ix_knowledge_mastery_user_id'), table_name='knowledge_mastery')
    op.drop_table('knowledge_mastery')

"""agrega tablas de wishlist

Revision ID: f4b8c72a91d3
Revises: e6a1c8d3f942
Create Date: 2026-08-19 12:00:00.000000

Listas de favoritos por cliente. wishlist_collections tiene una fila 'Favoritos'
(is_default=true) por cliente, garantizada unica a nivel de BD via un indice unico
parcial (mismo patron que ix_client_addresses_one_default) - se crea de forma perezosa
en el primer PUT /wishlist/favorites/{productUuid}, ver wishlist_service.py. Puede tener
ademas otras listas con nombre. wishlist_items es una tabla de toggle pura (sin uuid
propio, mismo patron que product_review_helpful_votes) - product_id sin ondelete porque
los productos nunca se hard-deletean (solo is_deleted=true). Ver CLAUDE.md, "Wishlist /
favoritos".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4b8c72a91d3'
down_revision: Union[str, Sequence[str], None] = 'e6a1c8d3f942'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('wishlist_collections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(), nullable=False),
    sa.Column('client_account_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['client_account_id'], ['client_accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_wishlist_collections_id'), 'wishlist_collections', ['id'], unique=False)
    op.create_index(op.f('ix_wishlist_collections_uuid'), 'wishlist_collections', ['uuid'], unique=True)
    op.create_index(op.f('ix_wishlist_collections_client_account_id'), 'wishlist_collections', ['client_account_id'], unique=False)
    op.create_index(
        'ix_wishlist_collections_one_default', 'wishlist_collections', ['client_account_id'],
        unique=True, postgresql_where=sa.text('is_default = true'),
    )

    op.create_table('wishlist_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('collection_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['collection_id'], ['wishlist_collections.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('collection_id', 'product_id', name='ux_wishlist_items_collection_product')
    )
    op.create_index(op.f('ix_wishlist_items_id'), 'wishlist_items', ['id'], unique=False)
    op.create_index(op.f('ix_wishlist_items_collection_id'), 'wishlist_items', ['collection_id'], unique=False)
    op.create_index(op.f('ix_wishlist_items_product_id'), 'wishlist_items', ['product_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_wishlist_items_product_id'), table_name='wishlist_items')
    op.drop_index(op.f('ix_wishlist_items_collection_id'), table_name='wishlist_items')
    op.drop_index(op.f('ix_wishlist_items_id'), table_name='wishlist_items')
    op.drop_table('wishlist_items')

    op.drop_index('ix_wishlist_collections_one_default', table_name='wishlist_collections')
    op.drop_index(op.f('ix_wishlist_collections_client_account_id'), table_name='wishlist_collections')
    op.drop_index(op.f('ix_wishlist_collections_uuid'), table_name='wishlist_collections')
    op.drop_index(op.f('ix_wishlist_collections_id'), table_name='wishlist_collections')
    op.drop_table('wishlist_collections')

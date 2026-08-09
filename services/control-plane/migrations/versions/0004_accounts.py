"""Add local accounts and revocable authenticated sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0004_accounts"
down_revision = "0003_user_input_continuations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    # ``sessions.user_id`` was reserved in the P0 schema. Keeping it in place
    # preserves every guest/project foreign key while activating account-level
    # ownership through the original project's owner session.
    op.add_column(
        "sessions",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_sessions_user_expires",
        "sessions",
        ["user_id", "expires_at"],
    )


def downgrade() -> None:
    raise RuntimeError("accounts and revoked sessions are durable identity history")

"""Freeze the model runtime contract on every run."""

import sqlalchemy as sa
from alembic import op

revision = "0005_run_runtime_contract"
down_revision = "0004_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rows created before model selection existed must keep the historical
    # DeepSeek Flash / high / 200K execution tuple. New runs write their current
    # resolved contract explicitly through the repository.
    op.add_column(
        "runs",
        sa.Column(
            "runtime_profile_id",
            sa.String(64),
            nullable=False,
            server_default="deepseek-flash",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runtime_model_ref",
            sa.String(160),
            nullable=False,
            server_default="fomo-litellm/fomo-pi-flash",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runtime_thinking",
            sa.String(32),
            nullable=False,
            server_default="high",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runtime_context_window",
            sa.Integer(),
            nullable=False,
            server_default="200000",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runtime_policy_version",
            sa.String(64),
            nullable=False,
            server_default="direct-pi-legacy-v0",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runtime_run_max_tokens",
            sa.Integer(),
            nullable=False,
            server_default="400000",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runtime_inference_tpm_limit",
            sa.Integer(),
            nullable=False,
            server_default="1000000",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runtime_max_spend_micros",
            sa.BigInteger(),
            nullable=False,
            server_default="2000000",
        ),
    )


def downgrade() -> None:
    raise RuntimeError("run runtime contracts are immutable execution history")

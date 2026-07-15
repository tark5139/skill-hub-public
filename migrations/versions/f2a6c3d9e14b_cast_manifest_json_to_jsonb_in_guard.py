"""Cast manifest JSON to JSONB in the immutable Skill-version guard.

Revision ID: f2a6c3d9e14b
Revises: 8db5f0998fbf
Create Date: 2026-07-16 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2a6c3d9e14b"
down_revision: str | None = "8db5f0998fbf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_GUARD_FUNCTION_TEMPLATE = """
CREATE OR REPLACE FUNCTION skillhub_guard_skill_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.immutable THEN
      RAISE EXCEPTION 'immutable Skill version cannot be deleted';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.immutable THEN
    IF NEW.immutable IS DISTINCT FROM TRUE
       OR NEW.skill_id IS DISTINCT FROM OLD.skill_id
       OR NEW.upload_id IS DISTINCT FROM OLD.upload_id
       OR NEW.semver IS DISTINCT FROM OLD.semver
       OR NEW.sha256 IS DISTINCT FROM OLD.sha256
       OR NEW.artifact_key IS DISTINCT FROM OLD.artifact_key
       OR NEW.manifest_key IS DISTINCT FROM OLD.manifest_key
       OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
       OR {manifest_comparison}
       OR NEW.scan_status IS DISTINCT FROM OLD.scan_status
       OR NEW.signature_status IS DISTINCT FROM OLD.signature_status THEN
      RAISE EXCEPTION 'immutable Skill version payload cannot be changed';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
         (OLD.status = 'submitted' AND NEW.status IN ('approved', 'rejected'))
      OR (OLD.status = 'approved' AND NEW.status = 'published')
      OR (OLD.status = 'published' AND NEW.status = 'deprecated')
    ) THEN
      RAISE EXCEPTION 'invalid immutable Skill lifecycle transition';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
"""


def _guard_function_sql(manifest_comparison: str) -> str:
    return _GUARD_FUNCTION_TEMPLATE.format(manifest_comparison=manifest_comparison)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            _guard_function_sql(
                "NEW.manifest_json::jsonb IS DISTINCT FROM OLD.manifest_json::jsonb"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            _guard_function_sql(
                "NEW.manifest_json IS DISTINCT FROM OLD.manifest_json"
            )
        )

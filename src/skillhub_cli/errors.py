"""CLI domain errors."""


class SkillHubError(RuntimeError):
    """Base error for expected, user-actionable CLI failures."""


class RegistryError(SkillHubError):
    """Registry transport or contract failure."""


class IntegrityError(SkillHubError):
    """Artifact, manifest, or installed-tree integrity failure."""


class InstallConflict(SkillHubError):
    """An unmanaged or locally modified installation would be overwritten."""


class StateError(SkillHubError):
    """Local state is missing, corrupt, or inconsistent."""

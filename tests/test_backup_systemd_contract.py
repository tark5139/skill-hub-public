from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = ROOT / "ops" / "tencent" / "systemd"
AWS_CONFIG = ROOT / "ops" / "tencent" / "aws-config"
BOOTSTRAP = ROOT / "ops" / "tencent" / "bootstrap-lighthouse.sh"


def test_backup_service_uses_stable_root_owned_operation_and_requires_cos() -> None:
    service = (SYSTEMD_DIR / "skill-hub-backup.service").read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "User=root" in service
    assert "Group=root" in service
    assert "UMask=0077" in service
    assert 'Environment="REQUIRE_COS_BACKUP=1"' in service
    assert 'Environment="BACKUP_ENV_FILE=/etc/skill-hub/backup.env"' in service
    assert 'Environment="AWS_CONFIG_FILE=/etc/skill-hub/aws-config"' in service
    assert 'Environment="AWS_EC2_METADATA_DISABLED=true"' in service
    assert (
        'Environment="PATH=/snap/aws-cli/current/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:'
        '/sbin:/bin:/snap/bin"' in service
    )
    assert "ExecStart=/usr/local/lib/skill-hub-ops/backup.sh" in service
    assert "ConditionPath" not in service
    assert "EnvironmentFile=" not in service


def test_backup_timer_is_daily_persistent_and_jittered_in_shanghai() -> None:
    timer = (SYSTEMD_DIR / "skill-hub-backup.timer").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 02:15:00 Asia/Shanghai" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=10m" in timer
    assert "Unit=skill-hub-backup.service" in timer
    assert "WantedBy=timers.target" in timer


def test_backup_aws_cli_uses_cos_virtual_host_addressing() -> None:
    config = AWS_CONFIG.read_text(encoding="utf-8")

    assert "region = ap-shanghai" in config
    assert "addressing_style = virtual" in config


def test_bootstrap_installs_the_verified_aws_config() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    assert "AWS_CONFIG=/etc/skill-hub/aws-config" in bootstrap
    assert "verified release lacks a regular ops/tencent/aws-config file" in bootstrap
    assert (
        'install -m 0644 -o root -g root "${APP_DIR}/ops/tencent/aws-config" '
        '"$AWS_CONFIG"' in bootstrap
    )

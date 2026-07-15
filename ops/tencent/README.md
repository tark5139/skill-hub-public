# Tencent Shanghai Lighthouse operations

These files prepare the existing root `compose.yaml` for a single-host Tencent Cloud Shanghai MVP.
They do not create cloud resources, modify Tencent security groups, or contain credentials.

- `bootstrap-lighthouse.sh`: installs Docker/Compose, AWS CLI and `flock`, creates the deployment
  user/secure directories, imports independently obtained release public keys into that user's
  GnuPG keyring, and checks out only an exact signed tag whose fingerprint appears in the
  root-owned allowlist. Raw commits and branches are refused. Firewall changes are opt-in.
- `.env.example`: production application/Compose environment template.
- `trusted-tag-signers.example`: example full-fingerprint allowlist. Install the populated file as
  root-owned `/etc/skill-hub/trusted-tag-signers`; keep it separate from the public-key bundle.
- `deploy.sh`: deploys only an allowlisted signed tag. For existing installations it runs the
  stable backup before any fetch, checkout, image build, or target Compose command, then applies
  forward migrations, starts API/Worker, and verifies readiness.
- `healthcheck.sh`: retries local liveness and readiness checks without printing secrets.
- `rollback.sh`: verifies an allowlisted signed tag and completes the same stable pre-fetch backup
  before rolling application code back; it never attempts an automatic DB downgrade.
- `backup.sh` and `backup.env.example`: use Docker labels to dump the running PostgreSQL container
  without executing application-tree code, then optionally copy verified dumps to a separate COS
  bucket with a separate identity.
- `systemd/skill-hub-backup.service` and `systemd/skill-hub-backup.timer`: auditable root-owned
  templates that require the COS copy and run the stable backup operation daily at 02:15
  Asia/Shanghai, with persistent catch-up and up to ten minutes of randomized delay. Bootstrap does
  not activate them before backup credentials and one manual backup have been verified; follow the
  operator runbook to install and enable them.
- `restore.sh`: verifies a portable checksum, recreates the database, restores, migrates, then and
  only then starts API/Worker; any restore or migration failure leaves services stopped.
- `common.sh`: shared safe `.env` parsing, portable checksum verification, and exclusive `flock`
  helpers used by the operational commands.
- `cam-policy-application.json` and `cam-policy-backup.json`: replaceable least-privilege CAM
  templates scoped to the exact Shanghai buckets/prefixes used by the workload.

The operator runbook is `docs/deployment-tencent-shanghai-mvp.md`.

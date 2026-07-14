# macOS Developer ID release procedure

This procedure produces the Apple Silicon `skillctl` artifact for macOS 13 or later. An official
artifact is signed with a **Developer ID Application** certificate, uses the hardened runtime and a
secure timestamp, is accepted by Apple's notary service, passes Apple's online standalone-code
notarization check, and has a recorded SHA-256 digest. The definitive Gatekeeper evidence is a
quarantined download test on a clean supported Mac, not a local `spctl` result for the raw CLI.

The scripts deliberately stop before publication. They do not create or move a Git tag, create a
GitHub Release, or upload to a public repository.

## Security boundary

Two independent Apple credentials are required:

1. A Developer ID Application certificate and its private key sign the executable and optional
   disk image. Generate the certificate request on the signing Mac and have the Apple Developer
   Account Holder issue the certificate. Keep the private key in Keychain; export a password-
   protected PKCS#12 (`.p12`) only when a protected CI job needs it.
2. A notary credential submits the signed artifact to Apple. Local releases may use a Keychain
   profile. CI uses a team App Store Connect API `.p8` key, key ID, and issuer ID. The `.p8` key is
   not the code-signing private key.

Never commit a certificate, private key, password, base64-encoded credential, Keychain profile, or
notarization token. Do not place a real secret in an example command, issue, log, or chat message.

The release host must be Apple Silicon macOS with current Xcode command-line tools, Python 3.12,
`codesign`, `security`, `spctl`, `hdiutil`, `xcrun notarytool`, and `xcrun stapler`. Gatekeeper must
remain enabled. The build sets `MACOSX_DEPLOYMENT_TARGET=13.0` unless explicitly overridden.

## One-time local setup

Install the version-constrained build toolchain, disable PEP 517 build isolation only after
Hatchling is present, and confirm the exact identity label and Team ID:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: --constraint constraints.txt \
  build hatchling pyinstaller
.venv/bin/python -m pip install --no-build-isolation --constraint constraints.txt '.[dev]'
.venv/bin/python -m pip check
security find-identity -v -p codesigning
```

`constraints.txt` now pins Hatchling, Build, PyInstaller, and their macOS/Python 3.12 transitive
dependencies in addition to the runtime and test graph. This improves repeatability and prevents
PEP 517 from resolving an unconstrained temporary backend. It is a version constraint set, not a
hash-locked supply-chain guarantee.

The signing label has this form:

```text
Developer ID Application: ORGANIZATION NAME (TEAMID1234)
```

For local notarization, store credentials in Keychain. Omitting the password option causes
`notarytool` to request it without putting it in shell history:

```sh
xcrun notarytool store-credentials skillhub-notary \
  --apple-id APPLE_ID_EMAIL \
  --team-id TEAMID1234
xcrun notarytool history --keychain-profile skillhub-notary
```

Alternatively, keep a team API key in a user-only readable location outside the repository and set
`SKILLHUB_NOTARY_KEY`, `SKILLHUB_NOTARY_KEY_ID`, and `SKILLHUB_NOTARY_ISSUER`. Do not configure both
authentication modes in one run.

## Local release

Export only non-secret identifiers, select the output format, and run the orchestrator:

```sh
export SKILLHUB_CODESIGN_IDENTITY='Developer ID Application: ORGANIZATION NAME (TEAMID1234)'
export SKILLHUB_EXPECTED_TEAM_ID='TEAMID1234'
export SKILLHUB_EXPECTED_COMMIT='0123456789abcdef0123456789abcdef01234567'
export SKILLHUB_EXPECTED_REF='refs/heads/main'  # or an exact refs/tags/... ref
export SKILLHUB_EXPECTED_VERSION='0.1.0'
export SKILLHUB_NOTARY_PROFILE='skillhub-notary'
export SKILLHUB_RELEASE_FORMAT='zip'  # zip, dmg, or both
./ops/macos/release_cli.sh
```

Copy the expected commit, ref, and version from the already reviewed release record; do not infer
them inside the signing command. Before accessing the signing identity, the orchestrator requires
the full commit to equal `HEAD`, requires the exact ref to resolve to that commit (and an expected
branch to be checked out), compares both the declared and source-imported versions, and rejects any
tracked or untracked worktree change. Ignored build and artifact directories remain outside the
source-state check. It also forces both `skillhub` and `skillhub_cli` to resolve from that checkout's
`src/` tree and passes the same source path to PyInstaller, so an older same-version package left in
the virtual environment cannot be mislabeled with the reviewed commit.

For a team API key, replace the profile variable with paths and identifiers; the example contains
no real credential:

```sh
unset SKILLHUB_NOTARY_PROFILE
export SKILLHUB_NOTARY_KEY="$HOME/.private_keys/AuthKey_EXAMPLE.p8"
export SKILLHUB_NOTARY_KEY_ID='EXAMPLEKEY'
export SKILLHUB_NOTARY_ISSUER='00000000-0000-0000-0000-000000000000'
chmod 0600 "$SKILLHUB_NOTARY_KEY"
./ops/macos/release_cli.sh
```

The orchestrator runs these fail-closed stages:

1. PyInstaller builds and smoke-tests the arm64 CLI. For an official build it receives
   `--codesign-identity`, so collected Mach-O libraries are signed with the Developer ID identity
   while they are assembled rather than relying only on a final outer signature.
2. `codesign` applies the final CLI signature with a secure timestamp and hardened runtime, after
   which the signed executable is smoke-tested again.
3. Both direct ZIP and DMG notarization entry points require the exact approved
   `Developer ID Application: ...` identity. `verify_cli.sh` compares that full identity and the
   exact Team ID against the embedded CLI; the DMG path additionally checks its outer signature.
4. The signed ZIP, and optionally a signed UDZO DMG, is accepted only when its adjacent input
   checksum names that exact file and matches its bytes, then submitted with `notarytool --wait`.
   A DMG's outer signature must have the same exact approved Developer ID identity, Team ID, and
   secure timestamp before submission and again after stapling.
5. The notary result and log are retained. Only an `Accepted` status proceeds.
6. The CLI must pass `codesign -R="notarized" --check-notarization`, which forces Apple's online
   ticket check for standalone code. A raw CLI is intentionally not assessed with `spctl --type
   execute`, because Apple's standalone-code guidance uses `codesign` and `spctl` can reject code
   that is valid but is not an app. A DMG must additionally pass stapling validation and the disk-
   image `spctl --type open` assessment.
7. A SHA-256 checksum is generated for each suffix-free artifact.

The notarization script performs stapling, container assessment, online standalone-code checking,
and checksum generation against a hidden candidate. After every required check succeeds, it first
atomically publishes the checksum sidecar and then renames the suffix-free artifact as the final
commit point. A rejected or interrupted run therefore cannot leave a new official-looking artifact
without its checksum.

Official builds refuse to overwrite an existing signed input, suffix-free artifact, checksum, or
notary evidence for the same version. This preserves the relationship between a rejected Apple
submission, its result/log JSON, and the submitted input digest. Move the entire prior attempt to
an audited archive before deliberately retrying that version.

## Outputs and ticket behavior

For version `X.Y.Z`, releasable outputs are:

| File | Meaning |
|---|---|
| `artifacts/skillctl-X.Y.Z-macos13-arm64.zip` | Developer ID-signed and Apple-notarized ZIP |
| `artifacts/skillctl-X.Y.Z-macos13-arm64.zip.sha256` | ZIP digest |
| `artifacts/skillctl-X.Y.Z-macos13-arm64.dmg` | Optional signed, notarized, and stapled disk image |
| `artifacts/skillctl-X.Y.Z-macos13-arm64.dmg.sha256` | DMG digest |
| `artifacts/skillctl-X.Y.Z-macos13-arm64.notary-*.json` | Submission result and complete notary log |
| `artifacts/skillctl-X.Y.Z-macos13-arm64.provenance.json` | Atomic local/Actions source binding, notary evidence, and artifact digest record |

Files containing `-adhoc` or `-signed-unnotarized` are intermediate QA artifacts and are never
eligible for public distribution.

A ZIP is accepted by the Apple notary service, but **a ZIP archive itself cannot be stapled**.
Gatekeeper retrieves the published ticket online for its signed contents. The suffix-free ZIP is
therefore byte-for-byte identical to the accepted upload. Use the optional DMG when an offline-
verifiable stapled container is required: the workflow signs the DMG, submits that outermost
container, staples its ticket, and validates it.

Verify local evidence without bypassing Gatekeeper:

```sh
cd artifacts
shasum -a 256 -c skillctl-X.Y.Z-macos13-arm64.zip.sha256
work_dir=$(mktemp -d)
/usr/bin/ditto -x -k skillctl-X.Y.Z-macos13-arm64.zip "$work_dir"
codesign -vvvv -R="notarized" --check-notarization \
  "$work_dir/skillctl-X.Y.Z-macos13-arm64/skillctl"
xcrun stapler validate -v skillctl-X.Y.Z-macos13-arm64.dmg  # DMG only
spctl --assess --type open --context context:primary-signature --verbose=4 \
  skillctl-X.Y.Z-macos13-arm64.dmg                          # DMG only
```

For the final Gatekeeper gate, restore a fresh VM snapshot or use a supported Mac that has never
seen this product, then:

1. download the candidate through the intended public channel using Safari or another path that
   applies `com.apple.quarantine`;
2. confirm the downloaded ZIP or DMG has a quarantine attribute and verify its SHA-256;
3. unpack/open and install exactly as an end user would, without manually adding or removing
   extended attributes;
4. run `skillctl --version` and `skillctl doctor` and record the macOS version, artifact digest,
   quarantine observation, and outcome.

For the ZIP, leave networking enabled because its ticket cannot be stapled and must be retrieved
online. The stapled DMG may additionally be tested offline. Gatekeeper caches prior assessments,
which is why reruns must restore a clean VM snapshot. A release must never instruct users to
disable Gatekeeper or use `xattr` to remove quarantine.

## Protected GitHub Actions workflow

### Personal 1.0 governance

The sole Personal 1.0 approval identity is `tark5139`. It cannot simultaneously be the independent
Environment reviewer and the workflow requester, and self-approval must not be used as a substitute.
Therefore the checked-in Actions workflow is **dormant in Personal 1.0**: do not enable its
`macos-release` Environment and do not configure Apple variables or secrets for it until a second
independent reviewer is added.

Personal 1.0 official signing instead uses the local procedure above on the reviewed, clean source
ref. `release_cli.sh` atomically creates and finalizes provenance binding the full commit, ref,
version, constraints digest, submitted-input checksums, Apple submission IDs/statuses, and final
artifact digests. Apple rejection result/log evidence and a failed provenance record are retained.
After local verification, `tark5139` may authorize public distribution only per exact Skill version
under [`github-publication-policy.md`](github-publication-policy.md); this authorization does not
retroactively make the CI workflow independently approved.

### Future team-mode workflow

`.github/workflows/macos-release.yml` is manual (`workflow_dispatch`) and has only
`contents: read`. It prepares a short-lived Actions artifact and notarization evidence; it cannot
create a tag or GitHub Release. The job uses the GitHub-hosted `macos-15` arm64 image and actions
pinned to full commit SHAs. GitHub's current runner-image table maps the exact `macos-15` label to
arm64; the workflow nevertheless checks `uname -m` and fails closed if that mapping or runner
selection ever differs.

Each dispatch requires three independent binding inputs: the full 40-character expected commit,
the exact protected `refs/heads/...` or `refs/tags/...` ref, and the expected project version. The
job compares all three with the event, checked-out commit, and installed package before it can
import credentials. It emits a provenance JSON containing those values, the constraints digest,
workflow run identity, requested format, notarization submission IDs/statuses, submitted-input
digests, and suffix-free artifact digests. This record improves traceability but is not itself a
cryptographically signed attestation.

Only after a second independent reviewer is available, create an Environment named
`macos-release` and enable the workflow with all of these controls:

- allow deployments only from protected branches or tags;
- require an independent reviewer and prevent self-review;
- disallow administrator bypass of the protection rules;
- put every Apple secret in this Environment, not at repository or organization scope;
- for a private repository, confirm the GitHub plan supports the required Environment protection
  rules before treating this as an approval gate.

Configure these Environment variables:

| Variable | Content |
|---|---|
| `MACOS_TEAM_MODE_ENABLED` | Set to literal `true` only after independent-review controls are active |
| `APPLE_DEVELOPER_IDENTITY` | Exact `Developer ID Application: ... (TEAMID)` label |
| `APPLE_TEAM_ID` | Expected Apple Developer Team ID |
| `APPLE_NOTARY_KEY_ID` | Team App Store Connect API key ID |
| `APPLE_NOTARY_ISSUER_ID` | Team API issuer ID |

Configure these Environment secrets through the GitHub UI or an approved secret manager:

| Secret | Content |
|---|---|
| `APPLE_DEVELOPER_ID_P12_BASE64` | Base64 of the password-protected Developer ID `.p12` |
| `APPLE_DEVELOPER_ID_P12_PASSWORD` | Export password for that `.p12` |
| `APPLE_NOTARY_API_KEY_P8_BASE64` | Base64 of the team notary `.p8` key |

The workflow installs the explicitly constrained build toolchain, disables build isolation, and
checks the resulting environment before Apple secret values are referenced. It then decodes
credentials only under the ephemeral runner's temporary directory, imports the signing identity
into a temporary Keychain, checks the exact identity, performs the release, and deletes temporary
material before artifact upload.

This ordering reduces accidental exposure but **is not strong process isolation**: dependency and
project code installed earlier remains on the same runner and can execute during the official
PyInstaller build, which needs the Developer ID identity to sign collected Mach-O files. Environment
reviewers must therefore treat every change to source, build hooks, constraints, PyInstaller,
scripts, and workflow code as capable of reaching release credentials. A future higher-assurance
design should use a dedicated signing service or a separately attested build/sign handoff that can
still perform inside-out Developer ID signing; the present personal-MVP workflow does not claim
that boundary.

Do not add a `pull_request` trigger, reference this Environment from a PR workflow, or expose these
secrets to forked/untrusted code. A PR can change executable workflow or build code and therefore
must be merged through protected review before a separately approved manual run. Reviewers must
inspect all changes in `.github/workflows/`, `ops/macos/`, build metadata, and dependencies before
approving access to the Environment.

The job allows up to 150 minutes because `both` can make two independent notarization requests,
each with a 60-minute `notarytool` wait. Verified suffix-free artifacts are uploaded only after a
successful run. Provenance, notary result/log JSON, and the signed-input checksum are uploaded after
Apple rejection only when the credential-cleanup step succeeds; if cleanup fails, no later action
is invoked on that runner.

The Actions artifact is still not a publication authorization. A separate, human-approved process
must verify its SHA-256 and notary evidence against the approved source commit before creating a
tag or public GitHub Release.

## Acceptance checklist

- [ ] Source ref is protected, reviewed, and resolves to the expected commit.
- [ ] Worktree is clean and the provenance commit/ref/version equal the reviewed local inputs.
- [ ] Dispatch commit, full ref, and version match the provenance record.
- [ ] Developer ID identity and observed Team ID exactly match the approved values.
- [ ] Hardened runtime and secure timestamp checks pass.
- [ ] Notary status is `Accepted`; result and log JSON are retained.
- [ ] Standalone CLI passes `codesign -R="notarized" --check-notarization` online.
- [ ] DMG, if produced, passes `stapler validate` and Gatekeeper open assessment.
- [ ] SHA-256 file verifies and is recorded in the release approval.
- [ ] Clean-Mac quarantine installation test passes without a Gatekeeper bypass.
- [ ] Publication is separately authorized; this procedure created no tag or Release.

## Authoritative references

- [Apple: Developer ID](https://developer.apple.com/support/developer-id/)
- [Apple: Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
- [Apple: Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
- [Apple DTS: Testing a Notarised Product](https://developer.apple.com/forums/thread/130560)
- [Apple: Resolving common notarization issues](https://developer.apple.com/documentation/security/resolving-common-notarization-issues)
- [GitHub: Deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
- [GitHub: Secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub runner image labels](https://github.com/actions/runner-images)

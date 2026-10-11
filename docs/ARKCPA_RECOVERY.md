# CPA build, capture and working-login rollback

The source-owned recovery entrypoint is `scripts/arkcpa_recovery.py`. It is a
replacement candidate for the dedicated external `arkcpa-backup.sh`; committing
it does not install it, change the timer, restore production data or reconnect
OneDrive. Keep the incumbent timer until the replacement passes a real capture
and an operator updates the unit deliberately.

## Build contract

The Docker base is pinned by manifest digest. `requirements.ark.lock` pins the
resolved Linux/Python 3.13 dependency versions; `requirements.txt` pins SQLAlchemy
2.0.53 so existing plain `postgresql://` URLs continue to use installed psycopg2.
SQLAlchemy 2.1 changes that default to psycopg3. Deliberate upgrades must update
the resolution and pass the actual PostgreSQL driver/import checks. The build
smoke uses a transient synthetic signing key, never a persisted runtime key.

Build from the complete reviewed integration revision, including the already
deployed controller changes and schema migrations. An operations-only branch
image is build proof, not a production replacement. Debian package mirrors are
not snapshot-pinned, so a clean rebuild is not claimed to be bit-for-bit identical;
the recovery bundle carries exact current image bytes for that purpose.

`ARKCPA_WEB_IMAGE` selects the web image, falling back to legacy `ARKCPA_IMAGE`.
`ARKCPA_CONTROLLER_IMAGE` is independently required; it never follows a web
release implicitly. At the October 10 repair boundary, preserve controller image
`sha256:79ff85f76fd64634637a11bb1473862db4c22176efdd44f35fc12d59ef3f5b4a`.
Retain the deployed daily controller interval of 86400 seconds during integration.
Apply the web service only with `--no-deps --no-build`; this source change does
not authorize replacing the controller or database.

## Recovery configuration

Create a private JSON config outside source. Values below are examples and full
live IDs must be freshly inspected before an approved capture. The key path is
read internally and never copied into the archive. `project_dir` locates actual
deployment `.env`/Compose; `source_dir` locates clean immutable release source.
These are intentionally separate: the historical live checkout is not a record
of every deployed overlay. `source_role` distinguishes the next release source
from the current runtime images when taking a pre-release snapshot.

```json
{
  "project_dir": "/home/novaadmin/arkcpa-slowbooks",
  "source_dir": "/absolute/path/to/clean/reviewed/integration/worktree",
  "expected_source_head": "full-reviewed-commit-sha",
  "source_role": "reviewed release candidate; pre-release runtime images captured independently",
  "ark_files_root": "/home/novaadmin/arkcpa-runtime-files",
  "config_root": "/home/novaadmin/.config/arkcpa",
  "key_file": "/home/novaadmin/.config/arkcpa/recovery/backup-passphrase",
  "gpg_home": "/absolute/private/recovery/gnupg",
  "output_root": "/absolute/private/recovery/encrypted",
  "work_root": "/absolute/private/recovery/work",
  "database_prefix": "arkcpa",
  "containers": {
    "postgres": {"name": "arkcpa-slowbooks-postgres-1", "id": "64-character-current-container-id"},
    "app": {"name": "arkcpa-slowbooks-slowbooks-1", "id": "64-character-current-container-id"},
    "controller": {"name": "arkcpa-slowbooks-controller-worker-1", "id": "64-character-current-container-id"}
  }
}
```

```sh
python scripts/arkcpa_recovery.py plan --config /private/recovery-config.json
python scripts/arkcpa_recovery.py backup --config /private/recovery-config.json
python scripts/arkcpa_recovery.py verify --config /private/recovery-config.json --archive /private/recovery/encrypted/arkcpa-recovery-RUN.tar.gpg
```

Capture dynamically inventories the `arkcpa` control database and all `arkcpa_`
entity databases; a newly provisioned Enerteq database is included automatically.
It checks inventory and pinned running containers again before sealing. It saves:

- Custom-format DB dumps and role definitions without password hashes.
- Actual `.env`, Compose and the dedicated allowlist of configuration/manifests.
- Complete evidence files and app uploads, with byte-level SHA-256 manifests.
- A self-contained Git bundle of the selected clean source revision.
- The exact web, controller and PostgreSQL images, saved by ID without tags.

Only private temporary directories hold plaintext. The final encrypted archive is
decrypted again; extraction rejects paths outside its root, links and special
files, then verifies every payload file. It loads saved untagged images and
restores every database into a newly created PostgreSQL container with no network
and no host ports/volumes, using the captured PostgreSQL image. It requires
`pg_restore --exit-on-error` and matching table counts. The random restore
container and plaintext staging are removed on completion/failure. No production
database is a restore target.

The evidence, upload and configuration checks reject detected concurrent edits.
Each pg_dump has its own transaction snapshot. This does **not** create an atomic
snapshot across independent databases and files. Releases require an owner/session
and write-idle gate; sustained writes require a separately designed quiescence or
storage snapshot procedure. A detected change fails the capture, not silently
marks it complete. Allow enough free disk for approximately three times the
evidence/image/dump size during capture and decryption verification. The isolated
database restore uses 1 GiB tmpfs; exceeding it fails safely and requires a
reviewed capacity change, not a production fallback.

## Local and offsite receipts are separate

With no `remote_root`, a successful receipt is
`local_verified_remote_pending`, saved as `latest.local.receipt.json`. It never
claims offsite protection. To enable the existing remote after credential recovery,
add `"remote_root": "onedrive:05_Ark CPA/Recovery/Encrypted"`. A remote receipt is
written only after upload and read-back ciphertext SHA agreement. `rclone` can
map a name to many destination types, so `remote_verified` proves transport and
bytes, not geographical separation; the operator must confirm the destination
is a separate failure domain. An upload failure preserves the valid encrypted
local copy/receipt and writes a separate failure marker without changing the
last successful remote receipt. Publish failures to the existing monitoring
channel when installing the service; no new notification destination is assumed.

As of the October 11 repair, a fresh read-only `rclone about onedrive:` still
fails `invalid_grant`. The human action is, as `novaadmin` on ARK-1:

```sh
rclone config reconnect onedrive:
```

This requires the account owner's interactive Microsoft authorization. Agents
must not reconnect using copied credentials or silently switch accounts. After
that, run the reviewed complete backup with the approved remote and verify a
fresh receipt. Until then the October 7 offsite receipt is the last verified one.

## Working-login rollback

Do not restore the old `arkcpa.magaenergy.ai` callback. The last healthy canonical
web image before this repair is
`sha256:c5638352ebb8a782400059d7e86687636fa3aa69057d956f648b7dffdc9944f0`.
Retain the current Enerteq hostname, callback, Authentik allowlist and tunnel/DNS.

Prepare `scripts/arkcpa_rollback.py prepare` with `--compose`, `--environment`,
`--output`, that `--rollback-image`, the full `--expected-web-image` candidate ID
and the three `--app-container`, `--postgres-container`, `--controller-container`
names. This creates a secret-free web-image-only Compose override and a plan
pinning unchanged deployment files and the database/controller identities.
Immediately before an approved rollback run `check --plan PATH`. It refuses
configuration, candidate or preserved-container drift. The release operator then
runs the exact plan's web-only activation command with its controller image
environment; the tool itself never activates anything. Recheck active owners,
schema compatibility and pins immediately before mutation. Validate the public
login button/callback and authorized signed-in workflow afterwards.

The current recovery bundle deliberately excludes the encryption passphrase,
Authentik database/credentials, tunnel credentials, DNS state and general host
configuration. Their existing restricted recovery processes and independent key
escrow must be identified before claiming total-loss recovery. A successful DB
drill is not signed-in accounting, identity-provider or full disaster-recovery
acceptance. Do not restore or change identity/DNS for a web-image rollback.

# Ark CPA multi-entity workspace

Ark CPA is one Authentik application, one browser UI, and multiple named user
accounts. Jay and Maesa can each have entity-admin access, while the private
provisioning file marks Jay as the sole protected approver. Data separation is
by legal entity, not by person:

- the control database owns identities, entity memberships, evidence hashes,
  agent decisions, compliance state, and protected-action records;
- every personal, corporation, trust, or LLC ledger is a separate PostgreSQL
  database;
- every entity has a separate `_shared/Ark CPA/<slug>` Ark Files tree;
- a signed session or `X-Ark-Entity` header is always checked against a user
  membership or an explicit agent-token grant;
- archived entities cannot be selected, and dormant entities cannot auto-post.

The initial topology is five ledgers: personal, two Alberta corporations, one
Alberta family trust, and one dormant Texas LLC. Exact legal names and private
identifiers stay in the encrypted operator configuration or the source
documents; they do not belong in source control.

Copy `deploy/arkcpa-entities.example.json` to an encrypted operator path, replace
every placeholder with exact legal names, and validate without changes:

```bash
python scripts/provision_arkcpa_entities.py --config /secure/arkcpa-entities.json
```

The dry-run must list the intended entity database and Ark Files boundaries.
Only then provision them:

```bash
python scripts/provision_arkcpa_entities.py \
  --config /secure/arkcpa-entities.json \
  --apply
```

The provisioner is idempotent. An existing slug is updated in place only when
its database name and Ark Files path match the reviewed config; a mismatch is a
hard stop. Applying the config reconciles the named users and makes its single
`protected_approver` authoritative. It preserves ordinary access held by users
not listed in the config, but removes protected-approval authority from them.

Do not create a per-user stack, hostname, database, or Authentik application.

Access grants use an explicit object so ordinary administration and protected
approval cannot be confused:

```json
"access": {
  "jay": {"role": "admin", "protected_approver": true},
  "maesa": {"role": "admin", "protected_approver": false}
}
```

## Existing file intake

The current OneDrive `05_Ark CPA` tree is source material, not the canonical
runtime mount. Before activation, create a reviewed mapping from each exact
legal entity folder to its `_shared/Ark CPA/<slug>` destination. Copy files;
never move or delete the source. Record the source path, destination path,
size, and SHA-256 in an immutable import manifest, verify every copied hash,
then expose only the destination tree to Ark CPA as a read-only mount.

Do not guess a legal entity name, fiscal year end, account number, or filing
status from a folder label. Provisioning stays blocked until the operator has
confirmed those values.

## Controller worker activation

The normal web service exposes manual scan, review, and governance controls.
The scheduled worker is a separate Compose profile and therefore does not start
by accident:

```bash
docker compose -f docker-compose.ark.yml --profile controller config
docker compose -f docker-compose.ark.yml --profile controller up -d controller-worker
```

Start with `ARKCPA_CONTROLLER_ALLOW_POSTING=false` and
`ARKCPA_CONTROLLER_REFRESH_TAX=false`. Prove both configured model routes return
strict JSON, resolve to different model families, and cannot see raw document
text. Tax-source refresh and routine-pattern posting are separate gates. Neither
flag permits filing, elections, money movement, credential changes, final
close, payroll, or model promotion.

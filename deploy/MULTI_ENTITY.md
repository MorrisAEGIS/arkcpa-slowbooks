# Ark CPA multi-entity workspace

Ark CPA is one Authentik application, one browser UI, and multiple named user
accounts. Jay is the human admin and Maesa is a bookkeeper. Data separation is
by legal entity, not by person:

- the control database owns identities, entity memberships, evidence hashes,
  agent decisions, compliance state, and protected-action records;
- every personal, corporation, trust, or LLC ledger is a separate PostgreSQL
  database;
- every entity has a separate `_shared/Ark CPA/<slug>` Ark Files tree;
- a signed session or `X-Ark-Entity` header is always checked against a user
  membership or an explicit agent-token grant;
- archived entities cannot be selected, and dormant entities cannot auto-post.

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

Do not create a per-user stack, hostname, database, or Authentik application.

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

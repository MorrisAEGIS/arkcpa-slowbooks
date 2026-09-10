# Ark CPA multi-user workspace

Ark CPA serves one accounting workspace at `arkcpa.magaenergy.ai`. Each person
uses their own Authentik identity and their own Ark CPA user row; one shared
database and file store contain the company books.

The access layers have distinct jobs:

- Authentik verifies the person and requires membership in `ArkCPA Owners`.
- Ark CPA binds the first verified login to an explicit email invitation.
- Ark CPA roles (`admin`, `bookkeeper`, and `readonly`) control application
  access and preserve per-person audit attribution.
- The immutable Authentik `(issuer, subject)` pair controls every login after
  the invitation is claimed. Email changes cannot transfer a bound account.

Provision or reconcile the main application, access group, and environment:

```bash
python deploy/provision_authentik_oidc.py \
  --env-file /home/novaadmin/arkcpa-slowbooks/.env \
  --member-username maesa \
  --company-name "MAGA Energy" \
  --workspace-id maga-energy \
  --workspace-label "MAGA Energy books"
```

The command retains existing OIDC and application secrets, writes the env file
atomically at mode `0600`, and fails if a named Authentik user does not exist.
It deliberately does not create Ark CPA roles. An Ark CPA admin creates each
user in **Settings -> Users & access**, entering the same verified email and
choosing the least-privilege role. The first successful Authentik callback
claims that invitation exactly once.

Do not create a second hostname, Compose project, database, or OAuth application
for another employee. Multiple people belong in this shared workspace unless a
new legal entity requires a deliberately separate set of books.

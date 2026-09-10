# ArkCPA private workspaces

ArkCPA isolates each person's private books as a separate deployment boundary,
not as a `workspace_id` filter scattered across accounting queries.

Each workspace has its own:

- Authentik OAuth2 application, exact-owner group, and callback URI
- immutable local `(issuer, subject)` principal binding
- Compose project and PostgreSQL database volume
- upload and attachment volume
- backup volume
- session, settings-encryption, payroll-encryption, and database secrets
- public hostname and explicit CORS origin

This means a missed filter in an invoice, payroll, attachment, or reporting
route cannot expose another person's workspace: the other data is not present
in that process, database, or mounted file volume.

Provision a private workspace with the idempotent helper:

```bash
python deploy/provision_authentik_oidc.py \
  --env-file /secure/path/maesa.env \
  --slug arkcpa-maesa \
  --application-name "Ark CPA - Maesa" \
  --provider-name "Ark CPA Maesa OIDC" \
  --hostname maesacpa.magaenergy.ai \
  --owner-username maesa \
  --bootstrap-email maesa@magaenergy.ai \
  --group "ArkCPA Maesa Owners" \
  --workspace-id maesa-private \
  --workspace-label "Maesa's private books" \
  --company-name "Maesa Private Books" \
  --compose-project arkcpa-maesa \
  --publish-port 3334 \
  --postgres-user arkcpa_maesa \
  --postgres-db arkcpa_maesa
```

Then start that project from the same release checkout/image:

```bash
docker compose --env-file /secure/path/maesa.env \
  -f docker-compose.ark.yml up -d --wait
```

Do not add two private owners to one private workspace group. If books must be
shared, provision a third, intentionally shared stack with a distinct group,
database, volumes, hostname, and explicit member-management policy.

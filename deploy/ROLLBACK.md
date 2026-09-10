# Ark CPA rollback

The legacy ArkCPA source and service remain at `/home/novaadmin/ark-accounting`
and `ark-accounting.service`. The Cloudflare tunnel continues to target
`http://localhost:3333`, so rollback does not require an edge change.

```bash
sudo systemctl disable --now arkcpa-slowbooks.service
sudo systemctl enable --now ark-accounting.service
curl -fsS -H 'Host: arkcpa.magaenergy.ai' http://127.0.0.1:3333/login >/dev/null
```

Ark CPA PostgreSQL, upload, and backup volumes are deliberately preserved by
the stop operation. After the legacy app is healthy, verify the public hostname
in a browser before declaring rollback complete.

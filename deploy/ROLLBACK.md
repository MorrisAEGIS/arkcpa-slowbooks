# Ark CPA rollback

Rollback stays on the single Ark CPA SlowBooks stack. Do not resurrect the
retired `ark-accounting.service` or a second Maesa stack.

Before deployment, record the current immutable image ID, export every Ark CPA
PostgreSQL database, and copy the Compose environment to the encrypted operator
vault. If a release gate fails:

1. Stop only the Ark CPA Compose project.
2. Restore the pre-deploy database exports to a fresh rollback volume.
3. Pin `ARKCPA_IMAGE` to the recorded image ID.
4. Start the same project on loopback port 3333.
5. Prove Authentik login, Jay and Maesa isolation, each entity selector, trial
   balance, attachment access, and the public hostname before reopening use.

Never run `docker compose down -v` during rollback. The prior volumes and the
encrypted read-only export are the recovery boundary.

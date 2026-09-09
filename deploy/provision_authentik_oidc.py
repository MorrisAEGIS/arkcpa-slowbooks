#!/usr/bin/env python3
"""Provision Ark CPA OIDC in Authentik and safely update a local env file."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

MARKER = "ARKCPA_OIDC_CONFIG="
SHELL = r"""
import json, secrets
from authentik.core.models import Application
from authentik.flows.models import Flow
from authentik.policies.models import Policy, PolicyBinding
from authentik.providers.oauth2.models import OAuth2Provider, ClientType

source = OAuth2Provider.objects.get(name="Ark Hub OIDC")
auth_flow = Flow.objects.filter(slug="ark-hermes-authentication-flow").first() or source.authentication_flow
provider = OAuth2Provider.objects.filter(name="Ark CPA OIDC").first()
created = provider is None
redirects = [
    {"url": "https://arkcpa.magaenergy.ai/api/auth/authentik/callback", "matching_mode": "strict", "redirect_uri_type": "authorization"},
    {"url": "https://cpa.magaenergy.ai/api/auth/authentik/callback", "matching_mode": "strict", "redirect_uri_type": "authorization"},
    {"url": "http://127.0.0.1:3333/api/auth/authentik/callback", "matching_mode": "strict", "redirect_uri_type": "authorization"},
]
if provider is None:
    provider = OAuth2Provider.objects.create(
        name="Ark CPA OIDC",
        authorization_flow=source.authorization_flow,
        authentication_flow=auth_flow,
        invalidation_flow=source.invalidation_flow,
        client_type=ClientType.CONFIDENTIAL,
        client_id="arkcpa-" + secrets.token_urlsafe(18),
        client_secret=secrets.token_urlsafe(36),
        _redirect_uris=redirects,
        signing_key=source.signing_key,
        encryption_key=source.encryption_key,
        sub_mode=source.sub_mode,
        issuer_mode=source.issuer_mode,
        include_claims_in_id_token=True,
        grant_types=source.grant_types,
    )
else:
    provider.authentication_flow = auth_flow
    provider.client_type = ClientType.CONFIDENTIAL
    provider._redirect_uris = redirects
    provider.include_claims_in_id_token = True
    provider.save()
provider.property_mappings.set(source.property_mappings.all())
application, _ = Application.objects.update_or_create(
    slug="arkcpa",
    defaults={"name": "Ark CPA", "provider": provider, "meta_launch_url": "https://arkcpa.magaenergy.ai/"},
)
family_policy = Policy.objects.get(name="ARK Family members only")
PolicyBinding.objects.get_or_create(target=application, policy=family_policy, defaults={"order": 0, "enabled": True})
print("ARKCPA_OIDC_CONFIG=" + json.dumps({
    "created": created,
    "client_id": provider.client_id,
    "client_secret": provider.client_secret,
    "issuer": "https://auth.magaenergy.ai/application/o/arkcpa/",
    "policy": family_policy.name,
}))
"""


def _write_env(path: Path, values: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    output: list[str] = []
    for line in existing:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in pending:
                output.append(f"{key}={pending.pop(key)}")
                continue
        output.append(line)
    if output and output[-1] != "":
        output.append("")
    output.extend(f"{key}={value}" for key, value in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--container", default="authentik-server")
    args = parser.parse_args()
    process = subprocess.run(
        ["docker", "exec", args.container, "ak", "shell", "-c", SHELL],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise SystemExit(process.stderr)
    line = next(
        (item for item in process.stdout.splitlines() if item.startswith(MARKER)), ""
    )
    if not line:
        raise SystemExit("Authentik did not return the expected provision marker")
    config = json.loads(line.removeprefix(MARKER))
    _write_env(
        args.env_file,
        {
            "AUTHENTIK_OIDC_ENABLED": "true",
            "AUTHENTIK_OIDC_ISSUER": config["issuer"],
            "AUTHENTIK_OIDC_CLIENT_ID": config["client_id"],
            "AUTHENTIK_OIDC_CLIENT_SECRET": config["client_secret"],
            "AUTHENTIK_OIDC_REDIRECT_URI": "https://arkcpa.magaenergy.ai/api/auth/authentik/callback",
            "AUTHENTIK_OIDC_SCOPES": "openid profile email",
            "AUTHENTIK_OIDC_REQUIRED_GROUP": "ARK Family",
        },
    )
    print(
        json.dumps(
            {
                "ok": True,
                "created": config["created"],
                "application": "Ark CPA",
                "issuer": config["issuer"],
                "policy": config["policy"],
                "env_file": str(args.env_file),
                "credentials_written": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

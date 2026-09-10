#!/usr/bin/env python3
"""Provision the shared Ark CPA workspace and its Authentik access group.

The command is idempotent. Existing application credentials and workspace
secrets are retained. Named Authentik users become members of the one Ark CPA
application; Ark CPA then applies its own per-user role. Secrets are written
atomically at mode 0600 and are never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

MARKER = "ARKCPA_OIDC_CONFIG="
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SAFE_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_SAFE_DB = re.compile(r"^[a-zA-Z0-9_-]+$")
_SAFE_USERNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,99}$")

SHELL = r"""
import json, os, secrets
from authentik.core.models import Application, Group, User
from authentik.flows.models import Flow
from authentik.policies.expression.models import ExpressionPolicy
from authentik.policies.models import PolicyBinding
from authentik.providers.oauth2.models import OAuth2Provider, ClientType

spec = json.loads(os.environ["ARKCPA_PROVISION_SPEC"])
source = OAuth2Provider.objects.get(name="Ark Hub OIDC")
auth_flow = Flow.objects.filter(slug="ark-hermes-authentication-flow").first() or source.authentication_flow
owner = User.objects.get(username=spec["owner_username"])
if (owner.email or "").strip().lower() != spec["bootstrap_email"]:
    raise RuntimeError("Authentik owner email does not match the requested bootstrap email")
usernames = list(dict.fromkeys([spec["owner_username"]] + spec["member_usernames"]))
members = list(User.objects.filter(username__in=usernames))
found = {user.username for user in members}
missing = [username for username in usernames if username not in found]
if missing:
    raise RuntimeError("Authentik users not found: " + ", ".join(missing))

group, _ = Group.objects.get_or_create(name=spec["group"])
group.users.set(members)

provider = OAuth2Provider.objects.filter(name=spec["provider_name"]).first()
created = provider is None
redirects = [
    {"url": url, "matching_mode": "strict", "redirect_uri_type": "authorization"}
    for url in spec["redirect_uris"]
]
if provider is None:
    provider = OAuth2Provider.objects.create(
        name=spec["provider_name"],
        authorization_flow=source.authorization_flow,
        authentication_flow=auth_flow,
        invalidation_flow=source.invalidation_flow,
        client_type=ClientType.CONFIDENTIAL,
        client_id=spec["slug"] + "-" + secrets.token_urlsafe(18),
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
    slug=spec["slug"],
    defaults={
        "name": spec["application_name"],
        "provider": provider,
        "meta_launch_url": "https://" + spec["hostname"] + "/",
    },
)
policy_name = spec["group"] + " only"
expression = "return ak_is_group_member(request.user, name=" + json.dumps(spec["group"]) + ")"
policy, _ = ExpressionPolicy.objects.update_or_create(
    name=policy_name,
    defaults={"expression": expression, "execution_logging": True},
)
PolicyBinding.objects.filter(target=application).exclude(policy=policy).delete()
PolicyBinding.objects.get_or_create(
    target=application,
    policy=policy,
    defaults={"order": 0, "enabled": True},
)
print("ARKCPA_OIDC_CONFIG=" + json.dumps({
    "created": created,
    "client_id": provider.client_id,
    "client_secret": provider.client_secret,
    "issuer": "https://auth.magaenergy.ai/application/o/" + spec["slug"] + "/",
    "policy": policy.name,
    "group_members": list(group.users.values_list("username", flat=True)),
}))
"""


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value
    return values


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
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
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


def _random_fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _validate(args: argparse.Namespace) -> None:
    if not _SAFE_SLUG.fullmatch(args.slug):
        raise SystemExit("--slug must contain only lowercase letters, digits, and hyphens")
    if not _SAFE_HOST.fullmatch(args.hostname):
        raise SystemExit("--hostname is not a safe DNS hostname")
    if any(not _SAFE_HOST.fullmatch(host) for host in args.alias):
        raise SystemExit("Every --alias must be a safe DNS hostname")
    if not _SAFE_SLUG.fullmatch(args.workspace_id) or not _SAFE_SLUG.fullmatch(
        args.compose_project
    ):
        raise SystemExit("Workspace and Compose project ids must be safe slugs")
    if not _SAFE_DB.fullmatch(args.postgres_user) or not _SAFE_DB.fullmatch(
        args.postgres_db
    ):
        raise SystemExit("PostgreSQL user/database must use letters, digits, _ or -")
    if not (1 <= args.publish_port <= 65535):
        raise SystemExit("--publish-port is outside the valid TCP range")
    if "@" not in args.bootstrap_email:
        raise SystemExit("--bootstrap-email must be an email address")
    usernames = [args.owner_username] + list(args.member_username)
    if any(not _SAFE_USERNAME.fullmatch(username) for username in usernames):
        raise SystemExit("Authentik usernames contain unsupported characters")


def _workspace_env(args: argparse.Namespace, oidc: dict[str, object]) -> dict[str, str]:
    current = _read_env(args.env_file)
    generated = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(48),
        "PAYROLL_ENCRYPTION_SECRET": secrets.token_urlsafe(48),
        "SESSION_SECRET_KEY": secrets.token_urlsafe(48),
        "SETTINGS_ENCRYPTION_KEY": _random_fernet_key(),
    }
    for key in tuple(generated):
        if current.get(key):
            generated[key] = current[key]
    return {
        "COMPOSE_PROJECT_NAME": args.compose_project,
        "APP_PUBLISH_PORT": str(args.publish_port),
        "APP_WORKERS": current.get("APP_WORKERS") or "2",
        "POSTGRES_USER": args.postgres_user,
        "POSTGRES_PASSWORD": generated["POSTGRES_PASSWORD"],
        "POSTGRES_DB": args.postgres_db,
        "PAYROLL_ENCRYPTION_SECRET": generated["PAYROLL_ENCRYPTION_SECRET"],
        "SESSION_SECRET_KEY": generated["SESSION_SECRET_KEY"],
        "SETTINGS_ENCRYPTION_KEY": generated["SETTINGS_ENCRYPTION_KEY"],
        "CORS_ALLOW_ORIGINS": ",".join(
            [f"https://{args.hostname}"] + [f"https://{item}" for item in args.alias]
        ),
        "SESSION_IDLE_TIMEOUT_SECONDS": current.get("SESSION_IDLE_TIMEOUT_SECONDS")
        or "14400",
        "AUTHENTIK_OIDC_ENABLED": "true",
        "AUTHENTIK_OIDC_ISSUER": str(oidc["issuer"]),
        "AUTHENTIK_OIDC_CLIENT_ID": str(oidc["client_id"]),
        "AUTHENTIK_OIDC_CLIENT_SECRET": str(oidc["client_secret"]),
        "AUTHENTIK_OIDC_REDIRECT_URI": f"https://{args.hostname}/api/auth/authentik/callback",
        "AUTHENTIK_OIDC_SCOPES": "openid profile email",
        "AUTHENTIK_OIDC_REQUIRED_GROUP": args.group,
        "AUTHENTIK_OIDC_BOOTSTRAP_EMAIL": args.bootstrap_email.lower(),
        "ARKCPA_WORKSPACE_ID": args.workspace_id,
        "ARKCPA_WORKSPACE_LABEL": args.workspace_label,
        "ARKCPA_PUBLIC_HOSTNAME": args.hostname,
        "COMPANY_NAME": args.company_name
        or current.get("COMPANY_NAME")
        or "My Company",
        "EMPLOYER_STATE": current.get("EMPLOYER_STATE") or "WA",
        "SUTA_RATE": current.get("SUTA_RATE") or "0.012",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--container", default="authentik-server")
    parser.add_argument("--slug", default="arkcpa")
    parser.add_argument("--application-name", default="Ark CPA")
    parser.add_argument("--provider-name", default="Ark CPA OIDC")
    parser.add_argument("--hostname", default="arkcpa.magaenergy.ai")
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--owner-username", default="jay")
    parser.add_argument("--member-username", action="append", default=[])
    parser.add_argument("--bootstrap-email", default="jay@magaenergy.ai")
    parser.add_argument("--group", default="ArkCPA Owners")
    parser.add_argument("--workspace-id", default="maga-energy")
    parser.add_argument("--workspace-label", default="MAGA Energy books")
    parser.add_argument("--company-name")
    parser.add_argument("--compose-project", default="arkcpa-slowbooks")
    parser.add_argument("--publish-port", type=int, default=3333)
    parser.add_argument("--postgres-user", default="arkcpa")
    parser.add_argument("--postgres-db", default="arkcpa")
    args = parser.parse_args()
    _validate(args)

    callback_hosts = [args.hostname] + list(args.alias)
    spec = {
        "slug": args.slug,
        "application_name": args.application_name,
        "provider_name": args.provider_name,
        "hostname": args.hostname,
        "owner_username": args.owner_username,
        "member_usernames": list(args.member_username),
        "bootstrap_email": args.bootstrap_email.strip().lower(),
        "group": args.group,
        "redirect_uris": [
            f"https://{host}/api/auth/authentik/callback" for host in callback_hosts
        ]
        + [f"http://127.0.0.1:{args.publish_port}/api/auth/authentik/callback"],
    }
    process = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            "ARKCPA_PROVISION_SPEC=" + json.dumps(spec, separators=(",", ":")),
            args.container,
            "ak",
            "shell",
            "-c",
            SHELL,
        ],
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
    oidc = json.loads(line.removeprefix(MARKER))
    _write_env(args.env_file, _workspace_env(args, oidc))
    print(
        json.dumps(
            {
                "ok": True,
                "created": oidc["created"],
                "application": args.application_name,
                "issuer": oidc["issuer"],
                "policy": oidc["policy"],
                "group": args.group,
                "group_members": oidc["group_members"],
                "workspace": args.workspace_id,
                "hostname": args.hostname,
                "env_file": str(args.env_file),
                "credentials_written": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Prepare/check a web-only rollback while retaining the working canonical login.

This tool never edits the deployment environment, identity provider or DNS and
never runs Compose up. The release operator activates the verified plan.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_environment(path):
    selected = {}
    keys = {"ARKCPA_PUBLIC_HOSTNAME", "AUTHENTIK_OIDC_REDIRECT_URI"}
    for line in Path(path).read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in keys:
            key = key.strip()
            if key in selected:
                raise ValueError("duplicate canonical environment field")
            selected[key] = value.strip().strip("\"'")
    host = selected.get("ARKCPA_PUBLIC_HOSTNAME")
    if host != "cpa.enerteqpower.com":
        raise ValueError("working Enerteq canonical hostname must be retained")
    callback = selected.get("AUTHENTIK_OIDC_REDIRECT_URI", "")
    parts = urlsplit(callback)
    if (
        parts.scheme != "https"
        or parts.netloc != host
        or parts.path != "/api/auth/authentik/callback"
        or parts.query
        or parts.fragment
    ):
        raise ValueError("working canonical callback must be retained")
    return {"hostname": host, "callback": callback}


def inspect(target, image=False):
    args = (
        ["docker", "image", "inspect", target]
        if image
        else ["docker", "inspect", target]
    )
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise ValueError("pinned Docker object is unavailable")
    return json.loads(result.stdout)[0]


def prepare(
    compose, environment, output, rollback_image, expected_web_image, containers
):
    for value in (rollback_image, expected_web_image):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("rollback and candidate images must be complete image IDs")
    if inspect(rollback_image, image=True)["Id"] != rollback_image:
        raise ValueError("rollback image identity differs")
    canonical = canonical_environment(environment)
    pinned = {}
    for kind in ("postgres", "controller"):
        data = inspect(containers[kind])
        pinned[kind] = {
            "name": containers[kind],
            "id": data["Id"],
            "image": data["Image"],
        }
    # JSON is valid YAML. No environment/secrets are copied into this override.
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    override = output / "rollback.compose.json"
    override.write_text(
        json.dumps({"services": {"slowbooks": {"image": rollback_image}}}, indent=2)
        + "\n"
    )
    os.chmod(override, 0o600)
    plan = {
        "schema": "arkcpa-working-login-rollback/v1",
        "canonical": canonical,
        "rollback_image": rollback_image,
        "expected_web_image": expected_web_image,
        "web_container": containers["app"],
        "preserved_containers": pinned,
        "files": {
            str(Path(p).resolve()): digest(p) for p in (compose, environment, override)
        },
        "command_environment": {
            "ARKCPA_CONTROLLER_IMAGE": pinned["controller"]["image"]
        },
        "activation_command": [
            "docker",
            "compose",
            "--env-file",
            str(Path(environment).resolve()),
            "-f",
            str(Path(compose).resolve()),
            "-f",
            str(override),
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "slowbooks",
        ],
        "identity_and_dns_mutation": False,
        "activation_requires": [
            "check plan against current runtime",
            "owner/session and database-idle gate",
            "compatible database schema",
            "verified recovery receipt",
            "browser login proof after activation",
        ],
    }
    plan_path = output / "rollback-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n")
    os.chmod(plan_path, 0o600)
    return plan


def check(plan):
    for path, expected in plan["files"].items():
        if digest(path) != expected:
            raise ValueError("deployment input changed; prepare a new rollback plan")
    for pinned in plan["preserved_containers"].values():
        current = inspect(pinned["name"])
        if current["Id"] != pinned["id"] or current["Image"] != pinned["image"]:
            raise ValueError("preserved container changed; rollback review required")
    if inspect(plan["web_container"])["Image"] != plan["expected_web_image"]:
        raise ValueError("web candidate changed; rollback review required")
    if inspect(plan["rollback_image"], image=True)["Id"] != plan["rollback_image"]:
        raise ValueError("rollback image unavailable")
    return {
        "pins_match": True,
        "canonical": plan["canonical"],
        "activation_performed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("prepare")
    for name in (
        "compose",
        "environment",
        "output",
        "rollback-image",
        "expected-web-image",
        "app-container",
        "postgres-container",
        "controller-container",
    ):
        create.add_argument("--" + name, required=True)
    verify = sub.add_parser("check")
    verify.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "check":
            result = check(json.loads(args.plan.read_text()))
        else:
            result = prepare(
                args.compose,
                args.environment,
                args.output,
                args.rollback_image,
                args.expected_web_image,
                {
                    "app": args.app_container,
                    "postgres": args.postgres_container,
                    "controller": args.controller_container,
                },
            )
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, KeyError):
        print(
            json.dumps(
                {"ok": False, "error": "rollback input or pin validation failed"}
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Provision the Authentik OIDC client for OpenSearch Dashboards. Idempotent.

Creates or updates: groups (opensearch-admins, opensearch-viewers), an OAuth2 provider,
the application, and group bindings so only members of those groups can sign in.

Runs from an operator machine, not the logging host:
  AUTHENTIK_URL=https://sso.orcastra.io AUTHENTIK_TOKEN=... \
    scripts/authentik-provision.py [--admins user1,user2] [--secret-out PATH|-] [--delete]

The client secret is written only to --secret-out ('-' = stdout, meant to be piped straight
into the logging host). It is never printed otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

APP_SLUG = "opensearch-dashboards"
APP_NAME = "OpenSearch Dashboards (logs.orcastra.io)"
PROVIDER_NAME = "Provider for opensearch-dashboards"
PUBLIC_URL = "https://logs.orcastra.io"
GROUPS = {
    "opensearch-admins": "Full access to logs.orcastra.io (OpenSearch all_access)",
    "opensearch-viewers": "Read-only access to logs.orcastra.io",
}
# Flow/key/mapping lookups by stable identifiers, so the script works on a rebuilt Authentik too.
AUTH_FLOW_SLUG = "default-provider-authorization-implicit-consent"
INVALIDATION_FLOW_SLUG = "default-provider-invalidation-flow"
SCOPES = ("openid", "email", "profile", "offline_access")


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/") + "/api/v3"
        self.token = token

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, allow_404: bool = False) -> Any:
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            # note: Cloudflare in front of Authentik rejects the default Python-urllib agent (error 1010)
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "orcastra-logging-provisioner/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if allow_404 and e.code == 404:
                return None
            sys.exit(f"ERROR {method} {path}: HTTP {e.code} {e.read().decode()[:500]}")

    def one(self, path: str, **query: str) -> dict[str, Any] | None:
        res = self.call("GET", f"{path}?{urllib.parse.urlencode(query)}")["results"]
        if len(res) > 1:
            sys.exit(f"ERROR ambiguous lookup {path} {query}: {len(res)} results")
        return res[0] if res else None


def log(msg: str) -> None:
    print(f"==> {msg}", file=sys.stderr)


def ensure_group(api: Api, name: str, desc: str, members: list[int] | None) -> str:
    g = api.one("/core/groups/", name=name)
    body: dict[str, Any] = {"name": name, "attributes": {"description": desc, "managed_by": "orcastra-logging"}}
    if g is None:
        g = api.call("POST", "/core/groups/", body)
        log(f"group {name} created")
    else:
        api.call("PATCH", f"/core/groups/{g['pk']}/", body)
        log(f"group {name} exists")
    for uid in members or []:
        # add_user is idempotent on Authentik's side
        api.call("POST", f"/core/groups/{g['pk']}/add_user/", {"pk": uid})
    return g["pk"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admins", default="", help="comma separated usernames added to opensearch-admins")
    ap.add_argument("--secret-out", default="", help="file path or '-' for stdout")
    ap.add_argument("--delete", action="store_true", help="remove application, provider and bindings (rollback)")
    a = ap.parse_args()

    api = Api(os.environ.get("AUTHENTIK_URL", "https://sso.orcastra.io"), os.environ["AUTHENTIK_TOKEN"])
    # note: /core/applications/ ignores a slug filter (returns every app), so fetch by path.
    app = api.call("GET", f"/core/applications/{APP_SLUG}/", allow_404=True)
    prov = api.one("/providers/oauth2/", name=PROVIDER_NAME)

    if a.delete:
        if app:
            api.call("DELETE", f"/core/applications/{APP_SLUG}/")
            log("application deleted (bindings go with it)")
        if prov:
            api.call("DELETE", f"/providers/oauth2/{prov['pk']}/")
            log("provider deleted")
        log("groups kept on purpose: memberships are owner data; delete them by hand if wanted")
        return

    admin_ids = []
    for u in filter(None, (x.strip() for x in a.admins.split(","))):
        user = api.one("/core/users/", username=u)
        if user is None:
            sys.exit(f"ERROR user not found: {u}")
        admin_ids.append(user["pk"])
    group_pks = {n: ensure_group(api, n, d, admin_ids if n == "opensearch-admins" else None) for n, d in GROUPS.items()}

    flow = api.one("/flows/instances/", slug=AUTH_FLOW_SLUG)
    inval = api.one("/flows/instances/", slug=INVALIDATION_FLOW_SLUG)
    key = api.one("/crypto/certificatekeypairs/", name="authentik Self-signed Certificate", has_key="true")
    mappings = []
    for s in SCOPES:
        m = api.one("/propertymappings/provider/scope/", managed=f"goauthentik.io/providers/oauth2/scope-{s}")
        if m is None:
            sys.exit(f"ERROR default scope mapping missing: {s}")
        mappings.append(m["pk"])
    if not (flow and inval and key):
        sys.exit("ERROR default flows or signing key not found")

    pbody = {
        "name": PROVIDER_NAME,
        "authorization_flow": flow["pk"],
        "invalidation_flow": inval["pk"],
        "client_type": "confidential",
        "signing_key": key["pk"],
        "property_mappings": mappings,
        "sub_mode": "hashed_user_id",
        "include_claims_in_id_token": True,
        "issuer_mode": "per_provider",
        # note: Dashboards refreshes with the refresh token; short access tokens bound how long a
        # removed user keeps working after their group membership is revoked.
        "access_token_validity": "minutes=10",
        "refresh_token_validity": "hours=12",
        "redirect_uris": [
            {"matching_mode": "strict", "url": f"{PUBLIC_URL}/auth/openid/login"},
            # post_logout_redirect_uri = opensearch_security.openid.base_redirect_url
            {"matching_mode": "strict", "url": PUBLIC_URL},
        ],
    }
    if prov is None:
        prov = api.call("POST", "/providers/oauth2/", pbody)
        log("provider created")
    else:
        prov = api.call("PATCH", f"/providers/oauth2/{prov['pk']}/", pbody)
        log("provider updated")

    abody = {
        "name": APP_NAME, "slug": APP_SLUG, "provider": prov["pk"],
        "meta_launch_url": PUBLIC_URL, "policy_engine_mode": "any",
        "meta_description": "Central log search for Orcastra, OrcaHub and other stacks",
    }
    if app is None:
        app = api.call("POST", "/core/applications/", abody)
        log("application created")
    else:
        app = api.call("PATCH", f"/core/applications/{APP_SLUG}/", abody)
        log("application updated")

    bound = {b.get("group") for b in api.call("GET", f"/policies/bindings/?target={app['pk']}&page_size=100")["results"]}
    for order, (name, gpk) in enumerate(group_pks.items()):
        if gpk not in bound:
            api.call("POST", "/policies/bindings/", {"target": app["pk"], "group": gpk, "order": order,
                                                     "enabled": True, "negate": False, "timeout": 30})
            log(f"binding {name} created")
        else:
            log(f"binding {name} exists")

    issuer = f"{api.base[:-len('/api/v3')]}/application/o/{APP_SLUG}/"
    log(f"client_id {prov['client_id']}")
    log(f"discovery {issuer}.well-known/openid-configuration")
    if a.secret_out:
        secret = api.call("GET", f"/providers/oauth2/{prov['pk']}/")["client_secret"]
        if a.secret_out == "-":
            sys.stdout.write(secret)
        else:
            fd = os.open(a.secret_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secret)
        log("client secret written to --secret-out")


if __name__ == "__main__":
    main()

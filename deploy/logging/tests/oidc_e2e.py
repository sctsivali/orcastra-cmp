#!/usr/bin/env python3
"""End-to-end SSO authorization test against a live OpenSearch + Authentik.

Creates throwaway Authentik users (admin / viewer / no group) with app passwords, gets real
tokens from the opensearch-dashboards provider (client_credentials grant), and checks what
OpenSearch lets each one do. Also proves that tokens from another Authentik provider, tampered
tokens, revoked group membership and deactivated users are rejected. Everything it creates is
deleted in a finally block.

  AUTHENTIK_TOKEN=... OS_URL=https://100.90.90.69:9200 OS_CA=certs/root-ca.pem tests/oidc_e2e.py

Read-only against existing data: the only OpenSearch writes go to a qa-oidc-* index it deletes.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

AK = os.environ.get("AUTHENTIK_URL", "https://sso.orcastra.io")
AK_TOKEN = os.environ["AUTHENTIK_TOKEN"]
OS_URL = os.environ.get("OS_URL", "https://100.90.90.69:9200")
OS_CA = os.environ.get("OS_CA", "certs/root-ca.pem")
SLUG = "opensearch-dashboards"
UA = "orcastra-logging-qa/1.0"  # Cloudflare rejects the default Python-urllib agent

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str) -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
         form: dict[str, str] | None = None, ctx: ssl.SSLContext | None = None) -> tuple[int, Any]:
    h = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")[:300]


def ak(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    return http(method, f"{AK}/api/v3{path}", body, {"Authorization": f"Bearer {AK_TOKEN}"})


def ak_ok(method: str, path: str, body: Any = None) -> Any:
    code, res = ak(method, path, body)
    if code >= 300:
        sys.exit(f"authentik {method} {path}: {code} {res}")
    return res


OS_CTX = ssl.create_default_context(cafile=OS_CA)


def os_call(method: str, path: str, token: str, body: Any = None) -> tuple[int, Any]:
    return http(method, OS_URL + path, body, {"Authorization": f"Bearer {token}"}, ctx=OS_CTX)


def claims(jwt: str) -> dict[str, Any]:
    p = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def get_token(client_id: str, client_secret: str, username: str, app_password: str) -> tuple[int, Any]:
    return http("POST", f"{AK}/application/o/token/", form={
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
        "username": username, "password": app_password, "scope": "openid profile email"})


def main() -> int:
    run = secrets.token_hex(3)
    created_users: list[int] = []
    foreign: dict[str, Any] = {}

    app = ak_ok("GET", f"/core/applications/{SLUG}/")
    prov = ak_ok("GET", f"/providers/oauth2/{app['provider']}/")
    cid, csec = prov["client_id"], prov["client_secret"]
    groups = {g["name"]: g["pk"] for g in ak_ok("GET", "/core/groups/?search=opensearch-&page_size=50")["results"]}

    def make_user(role: str, group: str | None) -> tuple[int, str, str]:
        uname = f"qa-os-{role}-{run}"
        u = ak_ok("POST", "/core/users/", {"username": uname, "name": f"QA OpenSearch {role}",
                                           "is_active": True, "path": "users/qa"})
        created_users.append(u["pk"])
        if group:
            ak_ok("POST", f"/core/groups/{groups[group]}/add_user/", {"pk": u["pk"]})
        ident = f"qa-os-{role}-{run}-app"
        ak_ok("POST", "/core/tokens/", {"identifier": ident, "intent": "app_password", "user": u["pk"],
                                        "description": "temporary QA token", "expiring": True})
        key = ak_ok("GET", f"/core/tokens/{ident}/view_key/")["key"]
        return u["pk"], uname, key

    try:
        print("== setup")
        admin_pk, admin_u, admin_k = make_user("admin", "opensearch-admins")
        viewer_pk, viewer_u, viewer_k = make_user("viewer", "opensearch-viewers")
        nog_pk, nog_u, nog_k = make_user("nogroup", None)

        print("== tokens from Authentik")
        code, t_admin = get_token(cid, csec, admin_u, admin_k)
        check(code == 200 and "id_token" in t_admin, "admin gets a token")
        code, t_viewer = get_token(cid, csec, viewer_u, viewer_k)
        check(code == 200 and "id_token" in t_viewer, "viewer gets a token")
        code, t_nog = get_token(cid, csec, nog_u, nog_k)
        check(code != 200, f"user outside the groups is refused by Authentik (HTTP {code})")
        c = claims(t_admin["id_token"])
        check(c.get("iss") == f"{AK}/application/o/{SLUG}/" and c.get("aud") in (cid, [cid]),
              "id_token issuer and audience are this provider")
        check("opensearch-admins" in c.get("groups", []), "groups claim present in id_token")
        check(c.get("preferred_username") == admin_u, "preferred_username claim is the username")

        A, V = t_admin["id_token"], t_viewer["id_token"]
        print("== admin (opensearch-admins -> all_access)")
        code, info = os_call("GET", "/_plugins/_security/authinfo", A)
        check(code == 200 and info.get("user_name") == admin_u and "all_access" in info.get("roles", []),
              f"authinfo: {info.get('user_name') if isinstance(info, dict) else info} roles={info.get('roles') if isinstance(info, dict) else ''}")
        idx = f"qa-oidc-{run}"
        check(os_call("PUT", f"/{idx}", A, {"settings": {"number_of_replicas": 0}})[0] == 200, "admin can create an index")
        check(os_call("POST", f"/{idx}/_doc?refresh=true", A, {"m": "qa", "@timestamp": "2026-10-04T00:00:00Z"})[0] == 201, "admin can write")
        check(os_call("GET", "/_plugins/_security/api/roles", A)[0] == 200, "admin can read the security API")

        print("== viewer (opensearch-viewers -> readall + read-only UI)")
        code, info = os_call("GET", "/_plugins/_security/authinfo", V)
        roles = info.get("roles", []) if isinstance(info, dict) else []
        check(code == 200 and {"readall", "kibana_user", "logs_readonly_ui"} <= set(roles), f"authinfo roles={roles}")
        code, res = os_call("GET", f"/{idx}/_search", V)
        check(code == 200 and res["hits"]["total"]["value"] == 1, "viewer can search")
        check(os_call("POST", f"/{idx}/_doc", V, {"m": "x"})[0] == 403, "viewer cannot write")
        check(os_call("DELETE", f"/{idx}", V)[0] == 403, "viewer cannot delete an index")
        check(os_call("PUT", f"/qa-oidc-viewer-{run}", V)[0] == 403, "viewer cannot create an index")
        check(os_call("GET", "/_plugins/_security/api/roles", V)[0] == 403, "viewer cannot read the security API")
        # System index protection answers either 403 or an empty, filtered result; any hit is a leak.
        code, res = os_call("GET", "/.opendistro_security/_search", V)
        hits = res.get("hits", {}).get("total", {}).get("value") if isinstance(res, dict) else None
        check(code in (403, 404) or (code == 200 and hits == 0),
              f"viewer cannot read the security index (HTTP {code}, hits={hits})")

        print("== rejected tokens")
        h, p, s = A.split(".")
        forged = claims(A); forged["groups"] = ["opensearch-admins"]; forged["preferred_username"] = "attacker"
        fp = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        check(os_call("GET", "/_plugins/_security/authinfo", f"{h}.{fp}.{s}")[0] == 401, "tampered token rejected")
        check(os_call("GET", "/_plugins/_security/authinfo", "not-a-jwt")[0] == 401, "garbage token rejected")

        # A provider for another app, signed with the same Authentik key: must not be accepted.
        flow = ak_ok("GET", "/flows/instances/?slug=default-provider-authorization-implicit-consent")["results"][0]["pk"]
        inval = ak_ok("GET", "/flows/instances/?slug=default-provider-invalidation-flow")["results"][0]["pk"]
        fprov = ak_ok("POST", "/providers/oauth2/", {
            "name": f"qa-os-foreign-{run}", "authorization_flow": flow, "invalidation_flow": inval,
            "client_type": "confidential", "signing_key": prov["signing_key"],
            "property_mappings": prov["property_mappings"], "redirect_uris": [{"matching_mode": "strict", "url": "https://qa.invalid/cb"}]})
        foreign["provider"] = fprov["pk"]
        fapp = ak_ok("POST", "/core/applications/", {"name": f"qa-os-foreign-{run}", "slug": f"qa-os-foreign-{run}", "provider": fprov["pk"]})
        foreign["app"] = fapp["slug"]
        code, t_foreign = get_token(fprov["client_id"], fprov["client_secret"], admin_u, admin_k)
        check(code == 200, "control: the foreign provider issues a token for the admin user")
        if code == 200:
            check(os_call("GET", "/_plugins/_security/authinfo", t_foreign["id_token"])[0] == 401,
                  "token from another Authentik app (same signing key) rejected")

        print("== revocation")
        ak_ok("POST", f"/core/groups/{groups['opensearch-viewers']}/remove_user/", {"pk": viewer_pk})
        code, t2 = get_token(cid, csec, viewer_u, viewer_k)
        if code == 200:
            code, info = os_call("GET", "/_plugins/_security/authinfo", t2["id_token"])
            r = info.get("roles", []) if isinstance(info, dict) else []
            check("readall" not in r and os_call("GET", f"/{idx}/_search", t2["id_token"])[0] == 403,
                  f"after removal from the group a new token has no access (roles={r})")
        else:
            check(True, f"after removal from the group Authentik refuses a token (HTTP {code})")
        ak_ok("PATCH", f"/core/users/{admin_pk}/", {"is_active": False})
        code, _ = get_token(cid, csec, admin_u, admin_k)
        check(code != 200, f"deactivated user cannot get a token (HTTP {code})")

        os_call("DELETE", f"/{idx}", A)
    finally:
        print("== cleanup")
        for pk in created_users:
            ak("DELETE", f"/core/users/{pk}/")
        if foreign.get("app"):
            ak("DELETE", f"/core/applications/{foreign['app']}/")
        if foreign.get("provider"):
            ak("DELETE", f"/providers/oauth2/{foreign['provider']}/")
        left = ak_ok("GET", f"/core/users/?search=qa-os-&page_size=50")["results"]
        print(f"  removed {len(created_users)} users; qa-os-* users left: {len([u for u in left if run in u['username']])}")

    failed = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

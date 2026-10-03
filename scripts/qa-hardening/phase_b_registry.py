"""Resolve public OCI metadata with fixed hosts and ephemeral anonymous pull auth."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json"
)
PUBLIC_REGISTRIES = {
    "docker.io": ("registry-1.docker.io", "https://auth.docker.io/token", "registry.docker.io"),
    "ghcr.io": ("ghcr.io", "https://ghcr.io/token", "ghcr.io"),
}
PUBLIC_REPOSITORIES = {
    ("docker.io", "library/postgres"), ("docker.io", "library/redis"),
    ("docker.io", "library/node"), ("docker.io", "library/python"),
    ("docker.io", "hashicorp/vault"), ("ghcr.io", "goauthentik/server"),
}
PUBLIC_BLOB_HOSTS = {
    "production.cloudflare.docker.com", "production.cloudfront.docker.com",
    "docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com",
    "pkg-containers.githubusercontent.com",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class PublicRegistry:
    """The short-lived token grants public pull only; never persist or log it."""

    def __init__(self, registry: str, repository: str):
        if (registry, repository) not in PUBLIC_REPOSITORIES:
            raise ValueError("Refusing a repository outside the public lab dependency allowlist")
        self.registry = registry
        self.repository = repository
        self.host, self.token_realm, self.service = PUBLIC_REGISTRIES[registry]
        self.opener = urllib.request.build_opener(NoRedirect)
        self.authorization = None

    def _request(self, url: str, *, authenticate: bool = True):
        headers = {"Accept": MEDIA_TYPES, "User-Agent": "orcastra-qa-hardening-public-provenance"}
        if authenticate and self.authorization:
            headers["Authorization"] = self.authorization
        try:
            return self.opener.open(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 307, 308) and f"/v2/{self.repository}/blobs/sha256:" in url:
                location = error.headers.get("Location", "")
                parsed = urllib.parse.urlsplit(location)
                if (parsed.scheme != "https" or parsed.hostname not in PUBLIC_BLOB_HOSTS
                    or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
                    raise RuntimeError("Refusing an unexpected public blob redirect") from None
                # important: public CDN requests never receive the registry Authorization header.
                request = urllib.request.Request(location, headers={"User-Agent": headers["User-Agent"]})
                return self.opener.open(request, timeout=30)
            challenge = error.headers.get("WWW-Authenticate", "")
            if error.code != 401 or not authenticate or self.authorization or f'realm="{self.token_realm}"' not in challenge:
                raise RuntimeError("Public dependency metadata was not available") from None
        token_url = self.token_realm + "?" + urllib.parse.urlencode({
            "service": self.service, "scope": f"repository:{self.repository}:pull",
        })
        with self._request(token_url, authenticate=False) as response:
            document = json.loads(response.read(1024 * 1024))
        token = document.get("token") or document.get("access_token")
        if not isinstance(token, str):
            raise TypeError("Anonymous public-pull auth was not available")
        self.authorization = "Bearer " + token
        return self._request(url)

    def metadata(self, kind: str, reference: str) -> tuple[dict, str, int]:
        if kind not in {"manifests", "blobs"} or not re.fullmatch(r"(?:[A-Za-z0-9_.-]{1,128}|sha256:[a-f0-9]{64})", reference):
            raise ValueError("Unexpected public manifest/config reference")
        with self._request(f"https://{self.host}/v2/{self.repository}/{kind}/{reference}") as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise RuntimeError("Public metadata exceeded the one-MiB limit")
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            declared = response.headers.get("Docker-Content-Digest")
        if reference.startswith("sha256:") and reference != digest or declared and declared != digest:
            raise RuntimeError("Public metadata digest verification failed")
        return json.loads(body), digest, len(body)

    def resolve(self, tag_or_digest: str) -> dict:
        index, index_digest, _ = self.metadata("manifests", tag_or_digest)
        candidates = [entry for entry in index.get("manifests", [])
                      if entry.get("platform", {}).get("architecture") == "amd64"
                      and entry.get("platform", {}).get("os") == "linux"
                      and not entry.get("platform", {}).get("variant")]
        if "manifests" in index:
            if len(candidates) != 1:
                raise RuntimeError("Exactly one linux/amd64 platform must be selected")
            manifest, platform_digest, manifest_bytes = self.metadata("manifests", candidates[0]["digest"])
        else:
            manifest, platform_digest, manifest_bytes = index, index_digest, 0
        config, config_digest, config_bytes = self.metadata("blobs", manifest["config"]["digest"])
        if config.get("architecture") != "amd64" or config.get("os") != "linux":
            raise RuntimeError("The config does not match the reviewed linux/amd64 platform")
        diff_ids = config.get("rootfs", {}).get("diff_ids", [])
        if len(diff_ids) != len(manifest["layers"]) or any(
            not re.fullmatch(r"sha256:[a-f0-9]{64}", digest) for digest in diff_ids
        ):
            raise RuntimeError("The official config must bind every uncompressed layer digest")
        return {
            "registry": self.registry, "repository": self.repository, "requested": tag_or_digest,
            "index_digest": index_digest, "platform": "linux/amd64", "manifest_digest": platform_digest,
            "manifest_bytes": manifest_bytes, "config_digest": config_digest, "config_bytes": config_bytes,
            "layers": [{"digest": layer["digest"], "size": layer["size"], "media_type": layer["mediaType"]}
                       for layer in manifest["layers"]],
            "uncompressed_layer_digests": diff_ids,
            "compressed_layer_bytes": sum(layer["size"] for layer in manifest["layers"]),
            "public_environment": [entry for entry in config.get("config", {}).get("Env", [])
                                   if entry.startswith(("NODE_VERSION=", "PG_MAJOR=", "PG_VERSION=", "PYTHON_VERSION="))],
            "verification": "Official public OCI manifest/config hashes verified over HTTPS; no layer download or installation",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", choices=PUBLIC_REGISTRIES)
    parser.add_argument("repository")
    parser.add_argument("tag_or_digest")
    args = parser.parse_args()
    print(json.dumps(PublicRegistry(args.registry, args.repository).resolve(args.tag_or_digest), indent=2))


if __name__ == "__main__":
    main()

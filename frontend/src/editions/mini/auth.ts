/**
 * Certificate sign-in for Mini: a NextAuth Credentials provider whose credential is the client
 * certificate nginx forwards, and session renewal by signing in again with the same certificate.
 *
 * nginx sends every request here with the browser's certificate in `x-ssl-client-cert` and its
 * own secret in `x-auth-proxy-secret`; a certificate header without that secret is never trusted.
 * The backend decides who the certificate belongs to and mints a token bound to it.
 *
 * important: imported by auth.ts, which middleware also bundles: nothing here may touch
 * server-only modules at import time. `next/headers` is loaded inside the renewal function.
 */

import Credentials from "next-auth/providers/credentials";
import type { JWT } from "next-auth/jwt";
import type { EditionAuth } from "@/lib/edition-auth";
import { REFRESH_FAILED, REFRESH_TOKEN_REVOKED } from "@/lib/auth-refresh";

const BACKEND_URL = process.env.INTERNAL_BACKEND_URL || "http://backend:4050";
const CERT_HEADER = "x-ssl-client-cert";
const SECRET_HEADER = "x-auth-proxy-secret";
const EDGE_HEADERS = ["x-ssl-client-cert", "x-ssl-client-verify", "x-ssl-client-s-dn", SECRET_HEADER];
const RETRY_AFTER_S = 30;
const LOGIN_TIMEOUT_MS = 15_000;

interface CertLogin {
  access_token: string;
  expires_at: number;
  role: string;
  email: string;
  display_name: string | null;
  groups: string[];
}

type HeaderSource = { get(name: string): string | null };

function secret(): string {
  return process.env.AUTH_PROXY_SECRET || "";
}

// Constant time on equal lengths; a length mismatch is already a refusal.
function sameSecret(provided: string | null, expected: string): boolean {
  if (!provided || !expected || provided.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i += 1) diff |= provided.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

/** The certificate nginx forwarded, only when nginx's secret came with it. */
export function edgeCertificate(source: HeaderSource): string | null {
  if (!sameSecret(source.get(SECRET_HEADER), secret())) return null;
  return source.get(CERT_HEADER) || null;
}

/**
 * nginx's view of the client, only when nginx's secret came with the request. The backend
 * rate-limits and audits on it; without it every sign-in and renewal would share this
 * container's address, and one caller's flood would lock everyone out.
 */
export function edgeClientIp(source: HeaderSource): string | null {
  if (!sameSecret(source.get(SECRET_HEADER), secret())) return null;
  const ip = source.get("x-real-ip");
  return ip && /^[0-9a-f:.]{2,45}$/i.test(ip) ? ip : null;
}

function clientIpHeaders(ip: string | null): Record<string, string> {
  return ip ? { "x-orcastra-client-ip": ip, "x-orcastra-client-ip-source": "edge" } : {};
}

type LoginOutcome = { ok: true; login: CertLogin } | { ok: false; revoked: boolean };

async function certLogin(certificate: string, clientIp: string | null): Promise<LoginOutcome> {
  try {
    const response = await fetch(`${BACKEND_URL}/api/v1/auth/cert-login`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        [CERT_HEADER]: certificate,
        [SECRET_HEADER]: secret(),
        ...clientIpHeaders(clientIp),
      },
      body: "{}",
      cache: "no-store",
      signal: AbortSignal.timeout(LOGIN_TIMEOUT_MS),
    });
    if (response.ok) return { ok: true, login: (await response.json()) as CertLogin };
    // Refused for the certificate itself (unknown, revoked, expired): the session is over.
    return { ok: false, revoked: response.status === 401 || response.status === 403 };
  } catch {
    return { ok: false, revoked: false };
  }
}

function tokenFields(login: CertLogin) {
  return {
    accessToken: login.access_token,
    expiresAt: login.expires_at,
    groups: login.groups,
    roles: login.groups.filter((g) => g.startsWith("role_")),
    authentikId: login.email,
    groupsReadAt: Math.floor(Date.now() / 1000),
    error: undefined,
    retryAfter: undefined,
  };
}

// One renewal per subject at a time: a page that polls the session from several tabs at once
// signs in once, and every waiter gets the same answer.
const inFlight = new Map<string, Promise<LoginOutcome>>();

async function renew(token: JWT): Promise<JWT | null> {
  const subject = token.authentikId ?? token.sub ?? "";
  const { headers } = await import("next/headers");
  const incoming = await headers();
  const certificate = edgeCertificate(incoming);
  if (!certificate) {
    // No certificate on this request (the browser stopped sending it, or the request did not
    // come through nginx): this session cannot be renewed.
    return { ...token, error: REFRESH_TOKEN_REVOKED };
  }
  let pending = inFlight.get(subject);
  if (!pending) {
    pending = certLogin(certificate, edgeClientIp(incoming)).finally(() => inFlight.delete(subject));
    inFlight.set(subject, pending);
  }
  const outcome = await pending;
  if (!outcome.ok) {
    return outcome.revoked
      ? { ...token, error: REFRESH_TOKEN_REVOKED }
      : { ...token, error: REFRESH_FAILED, retryAfter: Math.floor(Date.now() / 1000) + RETRY_AFTER_S };
  }
  // important: the certificate on this request must still be this session's operator. Another
  // operator's certificate in the same browser signs in fresh, never into this session.
  if (outcome.login.email !== token.authentikId) return { ...token, error: REFRESH_TOKEN_REVOKED };
  return { ...token, ...tokenFields(outcome.login) };
}

export const miniAuth: EditionAuth = {
  providers: [
    Credentials({
      id: "client-cert",
      name: "Certificate",
      credentials: {},
      async authorize(_credentials, request) {
        const certificate = edgeCertificate(request.headers);
        if (!certificate) return null;
        const outcome = await certLogin(certificate, edgeClientIp(request.headers));
        if (!outcome.ok) return null;
        const { login } = outcome;
        return {
          id: login.email,
          email: login.email,
          name: login.display_name || login.email,
          groups: login.groups,
          roles: login.groups.filter((g) => g.startsWith("role_")),
          // Carried to signedIn below through the user object; never into the session payload.
          accessToken: login.access_token,
          expiresAt: login.expires_at,
        } as never;
      },
    }),
  ],

  signedIn(token, user) {
    const u = user as { email: string; name?: string; groups: string[]; accessToken: string; expiresAt: number };
    return {
      ...token,
      ...tokenFields({
        access_token: u.accessToken, expires_at: u.expiresAt, role: "", email: u.email,
        display_name: u.name ?? null, groups: u.groups,
      }),
    };
  },

  refresh: renew,

  forwardHeaders(request, headers) {
    const certificate = edgeCertificate(request.headers);
    // Whatever the browser sent under these names is dropped; only nginx's copy goes on.
    for (const name of EDGE_HEADERS) headers.delete(name);
    // The client address goes on with or without a certificate, so a caller without one is
    // rate-limited on its own address instead of on the bucket every operator shares.
    for (const [name, value] of Object.entries(clientIpHeaders(edgeClientIp(request.headers)))) headers.set(name, value);
    if (!certificate) return;
    headers.set(CERT_HEADER, certificate);
    headers.set(SECRET_HEADER, secret());
  },
};

"use client";

import { useMemo, useState } from "react";
import {
  CheckCircle2,
  Eye,
  EyeOff,
  Loader2,
  Plug,
  ShieldAlert,
  XCircle,
} from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/hooks/use-toast";
import { api } from "@/lib/api";
import {
  formatApplicationType,
  isLegacyUnscopedKey,
} from "@/lib/integration-display";
import type { APIKeyCreateResponse, APIKeyValidateResponse } from "@/types";

// The base an integration should call.
//
// important: NEXT_PUBLIC_API_URL is the browser-visible BACKEND origin, and that is what belongs
// here. The dashboard origin also happens to work, because the Next proxy forwards the key
// headers and needs no session, but it is the wrong answer: every integration behind it shares
// one per-IP rate-limit bucket with every browser user.
//
// When it is unset the guide says so rather than guessing, because the obvious guess,
// window.location.origin, is precisely the dashboard.
function resolveBaseUrl(): string | null {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  return configured ? configured.replace(/\/+$/, "") : null;
}

const UNSET_BASE = "<your Orcastra API URL>";
const MASKED_SECRET = "oas_••••••••••••••••••••••••";

interface ApiKeyConnectionGuideProps {
  response: APIKeyCreateResponse;
}

export function ApiKeyConnectionGuide({
  response,
}: ApiKeyConnectionGuideProps) {
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<APIKeyValidateResponse | null>(null);
  // important: the secret is masked in the rendered snippet by default, matching the field above.
  // Without this the dialog masks the secret in one place and prints it in plain text two inches
  // below, so a screenshot taken by an operator who deliberately kept it hidden leaks it anyway.
  // Copy always copies the real value: hiding is about what is on screen, not what is pasted.
  const [revealed, setRevealed] = useState(false);

  const baseUrl = resolveBaseUrl();
  const base = baseUrl ?? UNSET_BASE;
  const key = response.api_key;

  const build = (secret: string) => {
    const id = response.api_key_id;
    return {
      curl: [
        `curl -s ${base}/api/v1/integrations/whoami \\`,
        `  -H "X-API-Key-ID: ${id}" \\`,
        `  -H "X-API-Key-Secret: ${secret}"`,
      ].join("\n"),
      env: [
        `ORCASTRA_API_URL=${base}`,
        `ORCASTRA_API_KEY_ID=${id}`,
        `ORCASTRA_API_KEY_SECRET=${secret}`,
      ].join("\n"),
      mcp: JSON.stringify(
        {
          mcpServers: {
            orcastra: {
              command: "orcastra-mcp",
              env: {
                ORCASTRA_API_URL: base,
                ORCASTRA_API_KEY_ID: id,
                ORCASTRA_API_KEY_SECRET: secret,
              },
            },
          },
        },
        null,
        2,
      ),
      http: [
        `GET ${base}/api/v1/integrations/instances HTTP/1.1`,
        `X-API-Key-ID: ${id}`,
        `X-API-Key-Secret: ${secret}`,
      ].join("\n"),
    };
  };

  // Two builds: one to copy, one to show. They differ only in the secret.
  const real = useMemo(
    () => build(response.api_key_secret),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [base, response.api_key_id, response.api_key_secret],
  );
  const shown = useMemo(
    () => (revealed ? real : build(MASKED_SECRET)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [revealed, real, base, response.api_key_id],
  );

  const runTest = async () => {
    setChecking(true);
    setResult(null);
    try {
      const answer = await api.integrations.validate({
        api_key_id: response.api_key_id,
        api_key_secret: response.api_key_secret,
      });
      setResult(answer);
      if (!answer.valid) {
        toast({
          title: "Key was refused",
          description: answer.error || "The server did not accept this key.",
          variant: "destructive",
        });
      }
    } catch (error) {
      toast({
        title: "Could not reach the server",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label className="flex items-center gap-2 text-base font-semibold">
          <Plug className="h-4 w-4" aria-hidden="true" />
          Connect with this key
        </Label>
        <p className="text-sm text-muted-foreground">
          Send both values as headers on every request. No sign-in and no
          session token is involved: these two headers are the whole credential.
        </p>
      </div>

      {!baseUrl && (
        <Alert variant="destructive">
          <ShieldAlert className="h-4 w-4" aria-hidden="true" />
          <AlertTitle>API URL is not configured</AlertTitle>
          <AlertDescription>
            NEXT_PUBLIC_API_URL is unset on this deployment, so the snippets
            below carry a placeholder. Replace it with the address the backend
            is published on.
          </AlertDescription>
        </Alert>
      )}

      <div className="rounded-md border p-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{formatApplicationType(key.app_type)}</Badge>
          {isLegacyUnscopedKey(key.capabilities) ? (
            <Badge variant="warning">Unscoped</Badge>
          ) : (
            (key.capabilities ?? []).map((capability) => (
              <Badge
                key={capability}
                variant="muted"
                className="font-mono font-normal"
              >
                {capability}
              </Badge>
            ))
          )}
        </div>
        <p className="mt-2 text-xs text-muted-foreground">
          {key.cluster_permissions.length} cluster(s).{" "}
          {key.expires_at
            ? `Expires ${new Date(key.expires_at).toLocaleDateString("en-GB", {
                day: "2-digit",
                month: "short",
                year: "numeric",
              })}.`
            : "Does not expire."}
        </p>
      </div>

      <Tabs defaultValue="curl">
        <div className="flex items-center justify-between gap-2">
          <TabsList>
            <TabsTrigger value="curl">curl</TabsTrigger>
            <TabsTrigger value="env">.env</TabsTrigger>
            <TabsTrigger value="mcp">MCP</TabsTrigger>
            <TabsTrigger value="http">HTTP</TabsTrigger>
          </TabsList>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => setRevealed((current) => !current)}
          >
            {revealed ? (
              <EyeOff className="h-3 w-3 mr-1" aria-hidden="true" />
            ) : (
              <Eye className="h-3 w-3 mr-1" aria-hidden="true" />
            )}
            {revealed ? "Hide secret" : "Show secret"}
          </Button>
        </div>
        <TabsContent value="curl" className="mt-3">
          <CodeBlock
            value={shown.curl}
            copyValue={real.curl}
            copyLabel="curl command"
            label="Confirms the key works and reports what it may do"
          />
        </TabsContent>
        <TabsContent value="env" className="mt-3">
          <CodeBlock
            value={shown.env}
            copyValue={real.env}
            copyLabel="Environment block"
            label="For a harness or a container"
          />
        </TabsContent>
        <TabsContent value="mcp" className="mt-3">
          <CodeBlock
            value={shown.mcp}
            copyValue={real.mcp}
            copyLabel="MCP configuration"
            label="For an MCP client config file"
          />
        </TabsContent>
        <TabsContent value="http" className="mt-3">
          <CodeBlock
            value={shown.http}
            copyValue={real.http}
            copyLabel="HTTP request"
            label="Reads instances across the key's clusters"
          />
        </TabsContent>
      </Tabs>

      <div className="space-y-2">
        <Button
          type="button"
          variant="outline"
          onClick={runTest}
          disabled={checking}
          className="w-full"
        >
          {checking ? (
            <>
              <Loader2
                className="h-4 w-4 mr-2 animate-spin"
                aria-hidden="true"
              />
              Testing...
            </>
          ) : (
            <>
              <Plug className="h-4 w-4 mr-2" aria-hidden="true" />
              Test connection
            </>
          )}
        </Button>

        {result && (
          <Alert variant={result.valid ? "default" : "destructive"}>
            {result.valid ? (
              <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            ) : (
              <XCircle className="h-4 w-4" aria-hidden="true" />
            )}
            <AlertTitle>
              {result.valid ? "Key accepted" : "Key refused"}
            </AlertTitle>
            <AlertDescription>
              {result.valid ? (
                <span>
                  Reaches {result.cluster_permissions.length} cluster(s)
                  {result.cluster_permissions.length > 0 && (
                    <>
                      {": "}
                      {result.cluster_permissions
                        .map(
                          (permission) =>
                            permission.cluster_name || permission.cluster_id,
                        )
                        .join(", ")}
                    </>
                  )}
                  .
                </span>
              ) : (
                <span>
                  {result.error || "The server did not accept this key."}
                </span>
              )}
            </AlertDescription>
          </Alert>
        )}
      </div>
    </div>
  );
}

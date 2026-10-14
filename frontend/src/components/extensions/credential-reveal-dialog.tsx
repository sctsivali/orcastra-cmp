"use client";

import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { publicApiBaseUrl, UNSET_PUBLIC_API_URL } from "@/lib/public-api-url";
import type { ExtensionInstallResponse } from "@/types/api";

const MASKED_KEY = "oas_••••••••••••••••••••••••";
const MASKED_SIGNING = "whsec_••••••••••••••••••••••••";

/**
 * The credential and the signing secret, shown once.
 *
 * Two values, deliberately separate. One authenticates the add-on to Orcastra; the other lets it
 * verify that a delivery came from Orcastra. They have different lifetimes and different blast
 * radii, so merging them into one blob would teach an author to treat them as interchangeable,
 * which is the mistake the platform's own design exists to avoid.
 *
 * Masked by default and copied in full. Hiding is about what a screenshot captures, not about
 * what lands on the clipboard, so a masked field whose copy button produces the mask would be
 * worse than no masking at all.
 */
export function CredentialRevealDialog({
  result,
  open,
  onClose,
}: {
  result: ExtensionInstallResponse | null;
  open: boolean;
  onClose: () => void;
}) {
  const [revealed, setRevealed] = useState(false);
  if (!result) return null;

  const base = publicApiBaseUrl() ?? UNSET_PUBLIC_API_URL;
  const curl = [
    `curl -s "${base}/api/v1/extensions/self" \\`,
    `  -H "X-API-Key-ID: ${result.api_key_id}" \\`,
    `  -H "X-API-Key-Secret: ${revealed ? result.api_key_secret : MASKED_KEY}"`,
  ].join("\n");
  const curlReal = curl.replace(MASKED_KEY, result.api_key_secret);

  const env = [
    `ORCASTRA_API_URL=${base}`,
    `ORCASTRA_API_KEY_ID=${result.api_key_id}`,
    `ORCASTRA_API_KEY_SECRET=${revealed ? result.api_key_secret : MASKED_KEY}`,
    `ORCASTRA_WEBHOOK_SECRET=${revealed ? result.webhook_signing_secret : MASKED_SIGNING}`,
  ].join("\n");
  const envReal = env
    .replace(MASKED_KEY, result.api_key_secret)
    .replace(MASKED_SIGNING, result.webhook_signing_secret);

  // Matches the documented recipe byte for byte, so an operator copying from here and a
  // developer copying from the docs end up running the same code.
  const verify = `import hmac, hashlib, time

def verify(raw_body: bytes, headers, secret: str) -> bool:
    ts = int(headers["X-Orcastra-Timestamp"])
    if abs(time.time() - ts) > 300:          # reject a replay
        return False
    expected = hmac.new(
        secret.encode(),
        f"v1.{ts}.".encode() + raw_body,     # the RAW bytes, never re-serialised JSON
        hashlib.sha256,
    ).hexdigest()
    for entry in headers["X-Orcastra-Signature"].split():
        if hmac.compare_digest(entry, f"v1,k={fingerprint(secret)}:{expected}"):
            return True
    return False`;

  const mcp = JSON.stringify(
    {
      mcpServers: {
        orcastra: {
          type: "http",
          url: `${base}/api/v1/mcp`,
          headers: {
            "X-API-Key-ID": result.api_key_id,
            "X-API-Key-Secret": revealed ? result.api_key_secret : MASKED_KEY,
          },
        },
      },
    },
    null,
    2,
  );
  const mcpReal = mcp.replace(MASKED_KEY, result.api_key_secret);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        className="max-w-3xl"
        onInteractOutside={(event) => event.preventDefault()}
        onEscapeKeyDown={(event) => event.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>Save These Now</DialogTitle>
          <DialogDescription>
            Neither value can be read again. If one is lost, rotate the credential rather than
            looking for it.
          </DialogDescription>
        </DialogHeader>

        <div className="flex justify-end">
          <Button variant="ghost" size="sm" onClick={() => setRevealed((r) => !r)}>
            {revealed ? (
              <>
                <EyeOff className="mr-2 h-4 w-4" aria-hidden="true" />
                Hide secrets
              </>
            ) : (
              <>
                <Eye className="mr-2 h-4 w-4" aria-hidden="true" />
                Reveal secrets
              </>
            )}
          </Button>
        </div>

        <Tabs defaultValue="env">
          <TabsList>
            <TabsTrigger value="env">Environment</TabsTrigger>
            <TabsTrigger value="curl">First Call</TabsTrigger>
            <TabsTrigger value="verify">Verify A Webhook</TabsTrigger>
            <TabsTrigger value="mcp">MCP Client</TabsTrigger>
          </TabsList>

          <TabsContent value="env" className="space-y-2">
            <CodeBlock value={env} copyValue={envReal} label=".env" copyLabel="Environment" />
            <p className="text-sm text-muted-foreground">
              The API URL is the backend origin, not the dashboard. An add-on calling through the
              dashboard shares one rate-limit bucket with every browser user.
            </p>
          </TabsContent>

          <TabsContent value="curl">
            <CodeBlock value={curl} copyValue={curlReal} label="bash" copyLabel="Command" />
          </TabsContent>

          <TabsContent value="verify" className="space-y-2">
            <CodeBlock value={verify} label="python" copyLabel="Snippet" />
            <p className="text-sm text-muted-foreground">
              Verify over the raw request body. Parsing the JSON and re-serialising it produces
              different bytes, and the signature will not match.
            </p>
          </TabsContent>

          <TabsContent value="mcp">
            <CodeBlock value={mcp} copyValue={mcpReal} label="json" copyLabel="Configuration" />
            <p className="mt-2 text-sm text-muted-foreground">
              A remote HTTP entry, not a spawned binary. Orcastra serves MCP itself at this URL.
            </p>
          </TabsContent>
        </Tabs>

        <DialogFooter>
          <Button onClick={onClose}>I Have Saved These</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

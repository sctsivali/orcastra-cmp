"use client";

import { useState } from "react";
import { Check, Copy, Download, AlertTriangle } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { saveBlob, type IssuedBundle } from "../api";

/**
 * A freshly issued certificate: the .p12 to download and the password that opens it, shown once.
 * Neither is kept anywhere: closing this view without saving both means issuing again.
 */
export function BundleReveal({ bundle, holder }: { bundle: IssuedBundle; holder: string }) {
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(bundle.password);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="space-y-4">
      <Alert>
        <AlertTriangle className="h-4 w-4" aria-hidden="true" />
        <AlertDescription>
          Save the file and its password now. Both are shown only once; neither is stored. Give the
          password to {holder} through a different channel from the file.
        </AlertDescription>
      </Alert>
      <div className="space-y-2">
        <Label htmlFor="bundle-password">Import Password</Label>
        <div className="flex items-center gap-2">
          <code id="bundle-password" translate="no"
            className="min-w-0 flex-1 break-all rounded-md border bg-muted/40 px-3 py-2 font-mono text-sm">
            {bundle.password}
          </code>
          <Button type="button" variant="outline" size="icon" onClick={copy}
            aria-label={copied ? "Password copied" : "Copy password"}>
            {copied ? <Check className="h-4 w-4" aria-hidden="true" /> : <Copy className="h-4 w-4" aria-hidden="true" />}
          </Button>
        </div>
        <p aria-live="polite" className="sr-only">{copied ? "Password copied" : ""}</p>
      </div>
      <Button type="button" className="w-full" onClick={() => { saveBlob(bundle.blob, bundle.filename); setSaved(true); }}>
        <Download className="mr-2 h-4 w-4" aria-hidden="true" />
        {saved ? `Download ${bundle.filename} Again` : `Download ${bundle.filename}`}
      </Button>
    </div>
  );
}

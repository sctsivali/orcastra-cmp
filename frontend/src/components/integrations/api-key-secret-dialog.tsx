"use client";

import { useState } from "react";
import { AlertTriangle, CheckCircle2, Copy, Eye, EyeOff } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiKeyConnectionGuide } from "@/components/integrations/api-key-connection-guide";
import { toast } from "@/hooks/use-toast";
import { copyText } from "@/lib/clipboard";
import type { APIKeyCreateResponse } from "@/types";

const MASKED_SECRET = "••••••••••••••••••••••••";

interface APIKeySecretDialogProps {
  open: boolean;
  onClose: () => void;
  response: APIKeyCreateResponse | null;
  title?: string;
  description?: string;
}

// The one time a key's secret is shown, on create and on rotate.
//
// note: two columns from `lg`. Stacked, the connection guide (the MCP snippets an operator came
// for) sat below the fold of a narrow dialog. The credentials column stays in view while the
// guide scrolls, so the secret and its warning are never scrolled away from.
export function APIKeySecretDialog({
  open,
  onClose,
  response,
  title = "API Key Created Successfully",
  description = "Save these credentials now. The secret will not be shown again.",
}: APIKeySecretDialogProps) {
  const [showSecret, setShowSecret] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  const copyToClipboard = async (text: string, type: string) => {
    const ok = await copyText(text);
    if (!ok) {
      toast({
        title: "Could not copy API key value",
        description: "Select and copy the value manually.",
        variant: "destructive",
      });
      return;
    }
    setCopied(type);
    setTimeout(() => setCopied(null), 2000);
    toast({
      title: "Copied",
      description: `API key ${type} copied to clipboard.`,
    });
  };

  if (!response) return null;

  const copiedIcon = (type: string) =>
    copied === type ? (
      <CheckCircle2 className="h-4 w-4 text-success" aria-hidden />
    ) : (
      <Copy className="h-4 w-4" aria-hidden />
    );

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-5xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogBody className="-mr-2 pr-2">
          <div className="grid gap-6 py-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <div className="space-y-4 lg:sticky lg:top-0 lg:self-start">
              <div className="space-y-2">
                <Label htmlFor="api-key-created-id">API Key ID</Label>
                <div className="flex gap-2">
                  <Input
                    id="api-key-created-id"
                    value={response.api_key_id}
                    readOnly
                    className="font-mono text-sm"
                  />
                  <Button
                    size="icon"
                    variant="outline"
                    aria-label="Copy key ID"
                    onClick={() => copyToClipboard(response.api_key_id, "ID")}
                  >
                    {copiedIcon("ID")}
                  </Button>
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="api-key-created-secret">API Key Secret</Label>
                <div className="flex gap-2">
                  <Input
                    id="api-key-created-secret"
                    value={showSecret ? response.api_key_secret : MASKED_SECRET}
                    readOnly
                    className="font-mono text-sm"
                  />
                  <Button
                    size="icon"
                    variant="outline"
                    aria-label={showSecret ? "Mask key secret" : "Reveal key secret"}
                    aria-pressed={showSecret}
                    onClick={() => setShowSecret(!showSecret)}
                  >
                    {showSecret ? <EyeOff className="h-4 w-4" aria-hidden /> : <Eye className="h-4 w-4" aria-hidden />}
                  </Button>
                  <Button
                    size="icon"
                    variant="outline"
                    aria-label="Copy key secret"
                    onClick={() => copyToClipboard(response.api_key_secret, "Secret")}
                  >
                    {copiedIcon("Secret")}
                  </Button>
                </div>
              </div>
              <Alert variant="warning" role="status">
                <AlertTriangle className="h-4 w-4" aria-hidden />
                <AlertTitle>Important</AlertTitle>
                <AlertDescription>
                  Copy and store these credentials securely. The secret cannot be retrieved later.
                </AlertDescription>
              </Alert>
            </div>
            {/* The credentials alone were the whole dialog, and a key holder had no way to learn
                the base URL, the header names, or which endpoints the key reaches. The obvious
                first call is a JWT route and answers 401, which reads as "this needs a login". */}
            <div className="min-w-0">
              <ApiKeyConnectionGuide response={response} />
            </div>
          </div>
        </DialogBody>
        <DialogFooter className="shrink-0 border-t pt-3">
          <Button onClick={onClose}>I have saved the credentials</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

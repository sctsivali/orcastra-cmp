"use client";

import type * as React from "react";
import { CodeBlock } from "@/components/ui/code-block";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

const KEYGEN = "ssh-keygen -t ed25519";
const SHOW_WINDOWS = "Get-Content $env:USERPROFILE\\.ssh\\id_ed25519.pub";
const SHOW_UNIX = "cat ~/.ssh/id_ed25519.pub";

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <li className="flex gap-2">
      <span className="w-4 shrink-0 text-right tabular-nums text-muted-foreground" aria-hidden="true">{n}.</span>
      <div className="min-w-0 flex-1 space-y-2">{children}</div>
    </li>
  );
}

function KeygenSteps({ shell, show }: { shell: string; show: string }) {
  return (
    <ol className="space-y-3 text-sm" aria-label={`Steps in ${shell}`}>
      <Step n={1}>
        <p>Open {shell} on your own computer and create a key. Press Enter to keep the default file.</p>
        <CodeBlock value={KEYGEN} copyLabel="Command" />
        <p className="text-xs text-muted-foreground">
          When it asks for a passphrase, type one. It protects the key if your computer is lost or stolen.
        </p>
      </Step>
      <Step n={2}>
        <p>Show the public key and copy the whole line it prints, starting with ssh-ed25519.</p>
        <CodeBlock value={show} copyLabel="Command" />
      </Step>
      <Step n={3}>
        <p>Paste that line above. Never paste the file without .pub: that one is your private key.</p>
      </Step>
    </ol>
  );
}

/** How to make a key on the person's own computer. Keys are never generated in the browser. */
export function KeyGuides() {
  return (
    <div className="space-y-3">
      <Tabs defaultValue="windows">
        <TabsList className="flex h-auto w-full flex-wrap justify-start">
          <TabsTrigger value="windows">Windows</TabsTrigger>
          <TabsTrigger value="unix">macOS and Linux</TabsTrigger>
          <TabsTrigger value="putty">PuTTYgen</TabsTrigger>
        </TabsList>
        <TabsContent value="windows" className="mt-3 space-y-2">
          <p className="text-xs text-muted-foreground">
            Windows 10 and 11 include OpenSSH. PowerShell needs no administrator rights for this.
          </p>
          <KeygenSteps shell="PowerShell" show={SHOW_WINDOWS} />
        </TabsContent>
        <TabsContent value="unix" className="mt-3">
          <KeygenSteps shell="Terminal" show={SHOW_UNIX} />
        </TabsContent>
        <TabsContent value="putty" className="mt-3">
          <ol className="space-y-3 text-sm" aria-label="Steps in PuTTYgen">
            <Step n={1}>
              <p>Open PuTTYgen. Under Parameters, choose EdDSA with the Ed25519 curve, then select Generate and move the mouse over the blank area until it finishes.</p>
            </Step>
            <Step n={2}>
              <p>Type a key passphrase, then save the private key (.ppk) somewhere only you can read. PuTTY uses that file to log in.</p>
            </Step>
            <Step n={3}>
              <p>
                Copy all the text in the box named Public key for pasting into OpenSSH authorized_keys file, and paste
                it above. It is one line starting with ssh-ed25519.
              </p>
            </Step>
          </ol>
        </TabsContent>
      </Tabs>
      <p className="text-xs text-muted-foreground">
        With a FIDO2 security key, create the key with <span className="font-mono" translate="no">ssh-keygen -t ed25519-sk</span> instead.
        Logging in then needs the security key plugged in.
      </p>
    </div>
  );
}

"use client";

import { useEffect, useRef, useState } from "react";
import { AlertCircle, Upload } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { capabilityNote } from "@/lib/extension-display";
import { formatLabel } from "@/lib/utils";
import type { ExtensionManifestInput, ExtensionVocabulary } from "@/types/api";

interface PublishAddonDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Served by the backend, so the picker never offers a value the server refuses. */
  vocabulary?: ExtensionVocabulary;
  pending: boolean;
  /** The server's refusal, shown verbatim. */
  error: string | null;
  onSubmit: (manifest: ExtensionManifestInput) => void;
}

const PERMISSIONS = ["read", "write", "admin"] as const;
const BREADTHS = ["selected", "all"] as const;

/** The server's 16 KB limit, checked here to save a round trip. */
const MAX_MANIFEST_BYTES = 16_000;

/**
 * Registers an add-on from a form or from a pasted or uploaded manifest.
 *
 * The form covers a first manifest without knowing the field names; the manifest tab accepts every
 * field, including settings and webhook defaults the form does not build. Both submit the same
 * document to the same endpoint, so validation is the server's either way.
 */
export function PublishAddonDialog({
  open,
  onOpenChange,
  vocabulary,
  pending,
  error,
  onSubmit,
}: PublishAddonDialogProps) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [publisher, setPublisher] = useState("");
  const [version, setVersion] = useState("1.0.0");
  const [description, setDescription] = useState("");
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [permission, setPermission] = useState<(typeof PERMISSIONS)[number]>("read");
  const [clusters, setClusters] = useState<(typeof BREADTHS)[number]>("selected");
  const [projects, setProjects] = useState<(typeof BREADTHS)[number]>("selected");

  const [raw, setRaw] = useState("");
  const [rawError, setRawError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  // important: claim a ref before the mutation rather than relying on the pending flag. Pending is
  // React state and does not reach the DOM until the next render, so two clicks in one tick would
  // both get through.
  const submitting = useRef(false);

  // Released once the request settles; otherwise a refused publish keeps the claim and every later
  // Publish returns silently. Still safe against a double click: two clicks in one tick both run
  // before any render, so this effect cannot fire between them.
  useEffect(() => {
    if (!pending) submitting.current = false;
  }, [pending]);

  const toggle = (capability: string) =>
    setCapabilities((current) =>
      current.includes(capability)
        ? current.filter((c) => c !== capability)
        : [...current, capability],
    );

  const claim = (): boolean => {
    if (submitting.current) return false;
    submitting.current = true;
    return true;
  };

  const submitForm = () => {
    if (!claim()) return;
    onSubmit({
      slug: slug.trim(),
      name: name.trim(),
      publisher: publisher.trim(),
      version: version.trim(),
      description: description.trim() || undefined,
      capabilities,
      scope: { permission, clusters, projects },
    });
  };

  const submitRaw = () => {
    setRawError(null);
    const bytes = new TextEncoder().encode(raw).length;
    if (bytes > MAX_MANIFEST_BYTES) {
      setRawError(`That manifest is ${bytes} bytes. The limit is ${MAX_MANIFEST_BYTES}.`);
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      // The parser's own message names the line, which is more use than "invalid JSON".
      setRawError(`That is not valid JSON. ${(e as Error).message}`);
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      setRawError("A manifest is a JSON object, not an array or a bare value.");
      return;
    }
    if (!claim()) return;
    onSubmit(parsed as ExtensionManifestInput);
  };

  const readFile = (file: File) => {
    setRawError(null);
    const reader = new FileReader();
    reader.onerror = () => setRawError("That file could not be read.");
    reader.onload = () => setRaw(String(reader.result ?? ""));
    reader.readAsText(file);
  };

  const close = (next: boolean) => {
    if (!next) submitting.current = false;
    onOpenChange(next);
  };

  const formIncomplete = !slug.trim() || !name.trim() || !publisher.trim() || !version.trim();

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="pr-8">
          <DialogTitle>Publish An Add-On</DialogTitle>
          <DialogDescription>
            This registers what your add-on is and what it asks for. It grants nothing on its own,
            and it starts as a draft that only you can see.
          </DialogDescription>
        </DialogHeader>

        <Tabs defaultValue="form" className="flex min-h-0 flex-1 flex-col">
          <TabsList className="w-fit">
            <TabsTrigger value="form">Form</TabsTrigger>
            <TabsTrigger value="manifest">Manifest File</TabsTrigger>
          </TabsList>

          <DialogBody>
            <TabsContent value="form" className="mt-0 space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="addon-slug">Slug</Label>
                  <Input
                    id="addon-slug"
                    value={slug}
                    onChange={(e) => setSlug(e.target.value)}
                    placeholder="acme-backup"
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono"
                  />
                  <p className="text-xs text-muted-foreground">
                    Lowercase letters, digits and hyphens. Permanent once published.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="addon-version">Version</Label>
                  <Input
                    id="addon-version"
                    value={version}
                    onChange={(e) => setVersion(e.target.value)}
                    placeholder="1.0.0"
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono"
                  />
                  <p className="text-xs text-muted-foreground">
                    Semantic version. Later versions must be greater.
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="addon-name">Name</Label>
                  <Input
                    id="addon-name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="Acme Backup"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="addon-publisher">Publisher</Label>
                  <Input
                    id="addon-publisher"
                    value={publisher}
                    onChange={(e) => setPublisher(e.target.value)}
                    placeholder="Acme Ltd"
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="addon-description">Description</Label>
                <Textarea
                  id="addon-description"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="What it does, in one line an operator will read while deciding."
                  rows={2}
                />
              </div>

              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">Capabilities</legend>
                <p className="text-xs text-muted-foreground">
                  Ask for the narrowest set that does the job. Every one appears on the consent
                  screen an operator reads before trusting you.
                </p>
                <ul className="space-y-2 rounded-md border p-3">
                  {(vocabulary?.capabilities ?? []).map((capability) => {
                    const note = capabilityNote(capability.elevated, capability.elevated_reason);
                    return (
                      <li key={capability.name} className="flex items-start gap-2">
                        <Checkbox
                          id={`cap-${capability.name}`}
                          checked={capabilities.includes(capability.name)}
                          onCheckedChange={() => toggle(capability.name)}
                          className="mt-0.5"
                        />
                        <Label
                          htmlFor={`cap-${capability.name}`}
                          className="cursor-pointer font-normal"
                        >
                          <span className="font-mono text-xs">{capability.name}</span>
                          <span className="block text-xs text-muted-foreground">
                            {capability.description}
                            {note ? ` ${note}` : ""}
                          </span>
                        </Label>
                      </li>
                    );
                  })}
                </ul>
              </fieldset>

              <div className="grid gap-4 sm:grid-cols-3">
                <div className="space-y-2">
                  <Label htmlFor="addon-permission">Permission</Label>
                  <Select
                    value={permission}
                    onValueChange={(v) => setPermission(v as (typeof PERMISSIONS)[number])}
                  >
                    <SelectTrigger id="addon-permission">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {PERMISSIONS.map((value) => (
                        <SelectItem key={value} value={value}>
                          {formatLabel(value)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="addon-clusters">Clusters</Label>
                  <Select
                    value={clusters}
                    onValueChange={(v) => setClusters(v as (typeof BREADTHS)[number])}
                  >
                    <SelectTrigger id="addon-clusters">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {BREADTHS.map((value) => (
                        <SelectItem key={value} value={value}>
                          {formatLabel(value)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="addon-projects">Projects</Label>
                  <Select
                    value={projects}
                    onValueChange={(v) => setProjects(v as (typeof BREADTHS)[number])}
                  >
                    <SelectTrigger id="addon-projects">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {BREADTHS.map((value) => (
                        <SelectItem key={value} value={value}>
                          {formatLabel(value)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                Scope describes the shape you are asking for, never a customer&apos;s cluster by
                name. What you actually reach is decided when an operator installs you.
              </p>
            </TabsContent>

            <TabsContent value="manifest" className="mt-0 space-y-3">
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => fileInput.current?.click()}
                >
                  <Upload className="mr-2 h-4 w-4" aria-hidden="true" />
                  Choose a file
                </Button>
                <input
                  ref={fileInput}
                  type="file"
                  accept="application/json,.json"
                  className="sr-only"
                  aria-label="Manifest JSON file"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) readFile(file);
                    // Reset so choosing the same file twice fires change again.
                    e.target.value = "";
                  }}
                />
                <span className="text-xs text-muted-foreground">
                  or paste it below. Nothing is uploaded until you publish.
                </span>
              </div>
              <div className="space-y-2">
                <Label htmlFor="addon-manifest">Manifest JSON</Label>
                <Textarea
                  id="addon-manifest"
                  value={raw}
                  onChange={(e) => setRaw(e.target.value)}
                  rows={14}
                  spellCheck={false}
                  className="font-mono text-xs"
                  placeholder={'{\n  "slug": "acme-backup",\n  "name": "Acme Backup",\n  "publisher": "Acme Ltd",\n  "version": "1.0.0",\n  "capabilities": ["inventory.read"],\n  "scope": {"permission": "read", "clusters": "selected", "projects": "selected"}\n}'}
                />
              </div>
              {rawError && (
                <Alert variant="destructive">
                  <AlertCircle className="h-4 w-4" aria-hidden="true" />
                  <AlertDescription>{rawError}</AlertDescription>
                </Alert>
              )}
              <p className="text-xs text-muted-foreground">
                This path accepts every manifest field, including settings and webhook defaults
                that the form does not build.
              </p>
            </TabsContent>

            {error && (
              <Alert variant="destructive" className="mt-4">
                <AlertCircle className="h-4 w-4" aria-hidden="true" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
          </DialogBody>

          <DialogFooter>
            <Button variant="outline" onClick={() => close(false)} disabled={pending}>
              Cancel
            </Button>
            <TabsContent value="form" className="mt-0">
              <Button onClick={submitForm} disabled={pending || formIncomplete}>
                {pending ? "Publishing" : "Publish"}
              </Button>
            </TabsContent>
            <TabsContent value="manifest" className="mt-0">
              <Button onClick={submitRaw} disabled={pending || !raw.trim()}>
                {pending ? "Publishing" : "Publish"}
              </Button>
            </TabsContent>
          </DialogFooter>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}

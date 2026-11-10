"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download, FileWarning, FileX2, Loader2, Save } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { OverflowText } from "@/components/ui/overflow-text";
import { useFileActions } from "@/hooks/use-instance-files";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { fileErrorCode, instanceFilesAPI, type GuestTarget } from "@/lib/instance-files/api";
import { clearDraft, draftKey, readDraft, writeDraft } from "@/lib/instance-files/draft-store";
import { guestText } from "@/lib/instance-files/guest-text";
import { fileContentKey } from "@/lib/instance-files/keys";
import { baseName } from "@/lib/instance-files/path";
import { describeReadError } from "@/lib/read-error";
import { formatBytes } from "@/lib/utils";
import type { CodeEditorHandle } from "./code-editor";
import { ImagePreview } from "./image-preview";
import type { OpenFile } from "./use-explorer-session";

const CodeEditor = dynamic(() => import("./code-editor"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center" role="status">
      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground motion-reduce:animate-none" aria-hidden="true" />
      <span className="sr-only">Loading the editor</span>
    </div>
  ),
});

interface FileViewerPaneProps {
  target: GuestTarget;
  file: OpenFile;
  writeReason?: string;
  onClose: () => void;
  onDownload: (path: string) => void;
}

/** What the editor opened on. `version` changes only when the editor should start again. */
interface OpenedText {
  text: string;
  etag: string;
  version: number;
}

/** The file instead of the list, inside the explorer, so it never fights the console for a layer. */
export function FileViewerPane({ target, file, writeReason, onClose, onDownload }: FileViewerPaneProps) {
  const { scopeKey } = useActorQueryScope();
  const actions = useFileActions(target);
  const editorRef = React.useRef<CodeEditorHandle>(null);
  const key = draftKey(target.host, target.project, target.instanceName, file.path);
  const content = useQuery({
    queryKey: fileContentKey(scopeKey, target, file.path),
    queryFn: ({ signal }) => instanceFilesAPI.readText(target, file.path, signal),
    enabled: file.kind === "text",
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
  });
  const [opened, setOpened] = React.useState<OpenedText | null>(null);
  const [dirty, setDirtyState] = React.useState(() => readDraft(key) !== undefined);
  const dirtyRef = React.useRef(dirty);
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState<string | null>(null);
  const [changedOnDisk, setChangedOnDisk] = React.useState<string | null>(null);
  const [offerInPlace, setOfferInPlace] = React.useState(false);
  const [confirmDiscard, setConfirmDiscard] = React.useState<"leave" | "reload" | null>(null);
  const inFlight = React.useRef(false);

  const setDirty = React.useCallback((value: boolean) => {
    dirtyRef.current = value;
    setDirtyState(value);
  }, []);

  // important: any change to a file in this guest refetches this query. Adopting a refetch while a
  // draft is open would move the etag under it, so Save would overwrite an unseen change, and
  // would remount the editor.
  React.useEffect(() => {
    const data = content.data;
    if (!data) return;
    setOpened((current) => {
      if (current?.etag === data.etag || (current && dirtyRef.current)) return current;
      const draft = readDraft(key);
      return {
        text: draft?.text ?? data.text,
        etag: draft?.etag ?? data.etag,
        version: (current?.version ?? 0) + 1,
      };
    });
  }, [content.data, key]);

  const name = guestText(baseName(file.path));
  const readOnly = Boolean(writeReason) || Boolean(content.data?.read_only);

  const save = React.useCallback(
    async (overrideEtag?: string, allowInPlace = false) => {
      const useEtag = overrideEtag ?? opened?.etag;
      const text = editorRef.current?.value();
      if (readOnly || !useEtag || text === undefined || inFlight.current) return;
      inFlight.current = true;
      setSaving(true);
      setSaveError(null);
      setChangedOnDisk(null);
      setOfferInPlace(false);
      try {
        const saved = await actions.save(file.path, text, useEtag, allowInPlace);
        if (saved) {
          setOpened((current) => current && { ...current, text, etag: saved.etag });
          // What was typed while this was on its way is unsaved, against the file just written.
          const now = editorRef.current?.value() ?? text;
          if (now === text) {
            setDirty(false);
            clearDraft(key);
          } else {
            writeDraft(key, { text: now, etag: saved.etag });
          }
        }
      } catch (error) {
        const code = fileErrorCode(error);
        const detail = (error as { detail?: { current_etag?: string } }).detail;
        if (code === "etag_mismatch") setChangedOnDisk(detail?.current_etag ?? "");
        else if (code === "atomic_replace_unavailable") setOfferInPlace(true);
        else setSaveError(error instanceof Error ? error.message : "This file could not be saved.");
      } finally {
        inFlight.current = false;
        setSaving(false);
      }
    },
    [actions, file.path, key, opened?.etag, readOnly, setDirty],
  );

  const leave = () => {
    if (dirty) setConfirmDiscard("leave");
    else onClose();
  };

  const showDiskVersion = async () => {
    const { data, error } = await content.refetch();
    if (!data || error) {
      setSaveError(error instanceof Error ? error.message : "This file could not be read.");
      return;
    }
    clearDraft(key);
    setDirty(false);
    setChangedOnDisk(null);
    setOpened((current) => ({ text: data.text, etag: data.etag, version: (current?.version ?? 0) + 1 }));
  };

  const code = fileErrorCode(content.error);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-[36px] items-center gap-2 border-b px-2">
        <Button variant="ghost" size="sm" className="h-7 gap-1 px-2" onClick={leave}>
          <ArrowLeft className="h-4 w-4" aria-hidden="true" /> Back to folder
        </Button>
        <OverflowText label="File name" value={`${name}${dirty ? " (unsaved)" : ""}`} mono mode="overlay" className="min-w-0 flex-1 text-xs" />
        {content.data && <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{formatBytes(content.data.size)}</span>}
        <Button variant="ghost" size="sm" className="h-7 gap-1 px-2" onClick={() => onDownload(file.path)}>
          <Download className="h-4 w-4" aria-hidden="true" /> Download
        </Button>
        {file.kind === "text" && !readOnly && (
          <Button size="sm" className="h-7 gap-1 px-2" disabled={!dirty || saving} onClick={() => void save()}>
            <Save className="h-4 w-4" aria-hidden="true" /> {saving ? "Saving" : "Save"}
          </Button>
        )}
      </div>
      {content.data?.read_only && (
        <Alert className="rounded-none border-x-0 border-t-0 py-2">
          <AlertDescription>This file is not valid UTF-8, so it opens read only. Saving would change bytes that cannot be shown here.</AlertDescription>
        </Alert>
      )}
      {writeReason && !content.data?.read_only && (
        <Alert className="rounded-none border-x-0 border-t-0 py-2"><AlertDescription>{writeReason}</AlertDescription></Alert>
      )}
      {changedOnDisk !== null && (
        <Alert className="rounded-none border-x-0 border-t-0 py-2">
          <AlertDescription className="flex flex-wrap items-center gap-2">
            This file changed after you opened it.
            <Button size="sm" variant="outline" className="h-7" onClick={() => setConfirmDiscard("reload")}>View disk version</Button>
            <Button size="sm" variant="destructive" className="h-7" onClick={() => void save(changedOnDisk)}>Overwrite</Button>
          </AlertDescription>
        </Alert>
      )}
      {offerInPlace && (
        <Alert className="rounded-none border-x-0 border-t-0 py-2">
          <AlertDescription className="flex flex-wrap items-center gap-2">
            This file cannot be replaced safely, for example because it is a mount. Writing directly risks a partial file if the connection drops.
            <Button size="sm" variant="destructive" className="h-7" onClick={() => void save(undefined, true)}>Write directly</Button>
          </AlertDescription>
        </Alert>
      )}
      {saveError && (
        <Alert variant="destructive" className="rounded-none border-x-0 border-t-0 py-2"><AlertDescription>{saveError}</AlertDescription></Alert>
      )}
      <div className="min-h-0 flex-1">
        {file.kind === "image" ? (
          <ImagePreview target={target} path={file.path} alt={name} />
        ) : opened ? (
          <CodeEditor
            key={opened.version}
            ref={editorRef}
            fileName={baseName(file.path)}
            initial={readDraft(key)?.text ?? opened.text}
            readOnly={readOnly}
            ariaLabel={`Contents of ${name}`}
            onChange={(text) => {
              setDirty(true);
              writeDraft(key, { text, etag: opened.etag });
            }}
            onSave={() => void save()}
          />
        ) : content.error ? (
          code === "too_large" ? (
            <EmptyState icon={FileWarning} title="This file is too large to open here" description="Download it instead." />
          ) : code === "not_text" ? (
            <EmptyState icon={FileWarning} title="This file is not text" description="It can be downloaded, not opened here." />
          ) : (
            <EmptyState icon={FileX2} title="This file could not be opened" description={describeReadError(content.error).reason} />
          )
        ) : (
          <div className="flex h-full items-center justify-center" role="status">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground motion-reduce:animate-none" aria-hidden="true" />
            <span className="sr-only">Loading the file</span>
          </div>
        )}
      </div>
      <DestructiveConfirmDialog
        open={confirmDiscard !== null}
        onOpenChange={(next) => !next && setConfirmDiscard(null)}
        title="Discard Changes"
        description={`Your edits to ${name} have not been saved.`}
        confirmLabel="Discard"
        pendingLabel="Discarding"
        pending={false}
        onConfirm={() => {
          const then = confirmDiscard;
          setConfirmDiscard(null);
          if (then === "reload") {
            void showDiskVersion();
            return;
          }
          clearDraft(key);
          onClose();
        }}
      />
    </div>
  );
}

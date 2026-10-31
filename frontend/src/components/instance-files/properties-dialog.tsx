"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OverflowText } from "@/components/ui/overflow-text";
import type { GuestEntry } from "@/lib/instance-files/api";
import { guestText } from "@/lib/instance-files/guest-text";
import { formatBytes, formatCount } from "@/lib/utils";
import { formatMode, formatTime } from "./file-table";

interface PropertiesDialogProps {
  entry: GuestEntry | null;
  path: string;
  writeReason?: string;
  onSave: (change: { mode?: number; uid?: number; gid?: number; deepMode: boolean; deepOwner: boolean }) => Promise<void>;
  onClose: () => void;
}

const WHO = ["Owner", "Group", "Everyone"] as const;
const BITS = [["Read", 4], ["Write", 2], ["Run", 1]] as const;

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0">{value}</dd>
    </>
  );
}

/** Exact facts, and permissions as both boxes and an octal value that stay in step. */
export function PropertiesDialog({ entry, path, writeReason, onSave, onClose }: PropertiesDialogProps) {
  const [mode, setMode] = React.useState(0);
  const [modeText, setModeText] = React.useState("");
  const [uid, setUid] = React.useState("");
  const [gid, setGid] = React.useState("");
  const [deepMode, setDeepMode] = React.useState(false);
  const [deepOwner, setDeepOwner] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [pending, setPending] = React.useState(false);
  const inFlight = React.useRef(false);

  React.useEffect(() => {
    if (!entry) return;
    setMode(entry.mode);
    setModeText(formatMode(entry.mode));
    setUid(String(entry.uid ?? ""));
    setGid(String(entry.gid ?? ""));
    setDeepMode(false);
    setDeepOwner(false);
    setError(null);
  }, [entry]);

  const isLink = entry?.type === "symlink";
  const locked = Boolean(writeReason) || isLink;

  const setBit = (shift: number, bit: number, on: boolean) => {
    const next = on ? mode | (bit << shift) : mode & ~(bit << shift);
    setMode(next);
    setModeText(formatMode(next));
  };

  const handleSave = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!entry || inFlight.current) return;
    if (!/^[0-7]{3,4}$/.test(modeText)) {
      setError("Permissions are three or four octal digits, for example 0644.");
      return;
    }
    if (!/^\d+$/.test(uid) || !/^\d+$/.test(gid)) {
      setError("Owner and group are numeric ids.");
      return;
    }
    const nextMode = parseInt(modeText, 8);
    const nextUid = Number(uid);
    const nextGid = Number(gid);
    // important: two ticks, not one. A single "everything inside" stamped the folder's owner and
    // mode on every child, whichever of the two the person meant to change.
    const newOwner = nextUid !== entry.uid || nextGid !== entry.gid;
    const change = {
      mode: nextMode !== entry.mode || deepMode ? nextMode : undefined,
      uid: newOwner || deepOwner ? nextUid : undefined,
      gid: newOwner || deepOwner ? nextGid : undefined,
      deepMode,
      deepOwner,
    };
    if (change.mode === undefined && change.uid === undefined) {
      onClose();
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      await onSave(change);
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "These changes could not be saved.");
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  };

  return (
    <Dialog open={entry !== null} onOpenChange={(next) => !next && !pending && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <form onSubmit={handleSave}>
          <DialogHeader className="pb-4">
            <DialogTitle>Properties</DialogTitle>
          </DialogHeader>
          <DialogBody>
            {entry && (
              <div className="space-y-4 text-sm">
                <dl className="grid grid-cols-[6rem_minmax(0,1fr)] gap-x-3 gap-y-1">
                  <Fact label="Path" value={<OverflowText label="Path" value={guestText(path)} mono />} />
                  <Fact label="Type" value={entry.type} />
                  <Fact label="Size" value={`${formatBytes(entry.size)} (${formatCount(entry.size)} ${entry.size === 1 ? "byte" : "bytes"})`} />
                  <Fact label="Modified" value={formatTime(entry.mtime)} />
                  {entry.link_target && <Fact label="Link Target" value={<OverflowText label="Link Target" value={guestText(entry.link_target)} mono />} />}
                </dl>
                {isLink ? (
                  <p className="text-muted-foreground">A link has no permissions of its own. Open its target to change them.</p>
                ) : (
                  <>
                    <fieldset disabled={locked} className="space-y-2">
                      <legend className="text-sm font-medium">Permissions</legend>
                      <div className="grid grid-cols-[5rem_repeat(3,3.5rem)] items-center gap-y-1 text-xs">
                        <span />
                        {BITS.map(([label]) => <span key={label} className="text-muted-foreground">{label}</span>)}
                        {WHO.map((who, row) => (
                          <React.Fragment key={who}>
                            <span>{who}</span>
                            {BITS.map(([label, bit]) => {
                              const shift = 6 - row * 3;
                              return (
                                <Checkbox
                                  key={label}
                                  aria-label={`${who} ${label}`}
                                  checked={Boolean(mode & (bit << shift))}
                                  onCheckedChange={(checked) => setBit(shift, bit, checked === true)}
                                />
                              );
                            })}
                          </React.Fragment>
                        ))}
                      </div>
                      <div className="w-32 space-y-1">
                        <Label htmlFor="instance-file-mode">Octal</Label>
                        <Input
                          id="instance-file-mode"
                          value={modeText}
                          className="font-mono"
                          inputMode="numeric"
                          onChange={(event) => {
                            setModeText(event.target.value);
                            if (/^[0-7]{3,4}$/.test(event.target.value)) setMode(parseInt(event.target.value, 8));
                          }}
                        />
                      </div>
                    </fieldset>
                    <fieldset disabled={locked} className="grid grid-cols-2 gap-3">
                      <div className="space-y-1">
                        <Label htmlFor="instance-file-uid">Owner ID</Label>
                        <Input id="instance-file-uid" value={uid} inputMode="numeric" onChange={(event) => setUid(event.target.value)} />
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor="instance-file-gid">Group ID</Label>
                        <Input id="instance-file-gid" value={gid} inputMode="numeric" onChange={(event) => setGid(event.target.value)} />
                      </div>
                    </fieldset>
                    {entry.type === "directory" && (
                      <div className="space-y-1">
                        <label className="flex items-center gap-2 text-sm">
                          <Checkbox checked={deepMode} disabled={locked} onCheckedChange={(checked) => setDeepMode(checked === true)} />
                          Apply these permissions to everything inside
                        </label>
                        <label className="flex items-center gap-2 text-sm">
                          <Checkbox checked={deepOwner} disabled={locked} onCheckedChange={(checked) => setDeepOwner(checked === true)} />
                          Apply this owner to everything inside
                        </label>
                        <p className="text-xs text-muted-foreground">Files and folders take the same values. Links are skipped.</p>
                      </div>
                    )}
                    {writeReason && <p className="text-xs text-muted-foreground">{writeReason}</p>}
                  </>
                )}
              </div>
            )}
          </DialogBody>
          <DialogSubmitError message={error} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={pending}>Close</Button>
            {!locked && <Button type="submit" disabled={pending}>{pending ? "Saving" : "Save"}</Button>}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

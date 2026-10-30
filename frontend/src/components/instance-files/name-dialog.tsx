"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DialogSubmitError } from "@/components/ui/dialog-submit-error";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface NameDialogProps {
  open: boolean;
  title: string;
  fieldLabel: string;
  submitLabel: string;
  initial: string;
  /** Rejected names answer here, before a request is sent. */
  validate?: (value: string) => string | null;
  onSubmit: (value: string) => Promise<void>;
  onClose: () => void;
}

// A name as a person would break it, said before it costs a round trip. The backend decides.
export function nameProblem(value: string): string | null {
  if (!value || value === "." || value === "..") return "Enter a name.";
  if (value.includes("/")) return "A name cannot contain a slash.";
  if (new TextEncoder().encode(value).length > 255) return "This name is too long.";
  return null;
}

/** New folder, new file, rename and "Move To": one field, one button, one place errors land. */
export function NameDialog({ open, title, fieldLabel, submitLabel, initial, validate = nameProblem, onSubmit, onClose }: NameDialogProps) {
  const [value, setValue] = React.useState(initial);
  const [error, setError] = React.useState<string | null>(null);
  const [pending, setPending] = React.useState(false);
  const inFlight = React.useRef(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (!open) return;
    setValue(initial);
    setError(null);
    setPending(false);
    requestAnimationFrame(() => {
      const input = inputRef.current;
      if (!input) return;
      input.focus();
      // A rename selects the stem, so typing keeps the extension.
      const dot = initial.lastIndexOf(".");
      input.setSelectionRange(0, dot > 0 ? dot : initial.length);
    });
  }, [open, initial]);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (inFlight.current) return;
    const problem = validate(value);
    if (problem) {
      setError(problem);
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      await onSubmit(value);
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "This could not be done.");
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !pending && onClose()}>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={handleSubmit}>
          <DialogHeader className="pb-4">
            <DialogTitle>{title}</DialogTitle>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-2">
              <Label htmlFor="instance-file-name">{fieldLabel}</Label>
              <Input
                id="instance-file-name"
                ref={inputRef}
                value={value}
                spellCheck={false}
                autoComplete="off"
                className="font-mono"
                onChange={(event) => setValue(event.target.value)}
              />
            </div>
          </DialogBody>
          <DialogSubmitError message={error} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={pending}>Cancel</Button>
            <Button type="submit" disabled={pending}>{pending ? "Working" : submitLabel}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

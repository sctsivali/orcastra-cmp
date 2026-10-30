"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { OverflowText } from "@/components/ui/overflow-text";
import { guestText } from "@/lib/instance-files/guest-text";
import { cn } from "@/lib/utils";

export type ConflictChoice = "keep" | "skip" | "replace";

interface ConflictDialogProps {
  names: string[];
  folder: string;
  total: number;
  onChoose: (choices: ReadonlyMap<string, ConflictChoice>) => void;
  onCancel: () => void;
}

const CHOICES: { value: ConflictChoice; label: string; description: string }[] = [
  { value: "keep", label: "Keep Both", description: "The new file gets a numbered name." },
  { value: "skip", label: "Skip", description: "Nothing changes for these files." },
  { value: "replace", label: "Replace", description: "The new file takes its place, keeping owner and permissions." },
];

const SHOWN = 200;

/** One question for a whole batch, asked before a byte is sent. */
export function ConflictDialog({ names, folder, total, onChoose, onCancel }: ConflictDialogProps) {
  const [perItem, setPerItem] = React.useState(false);
  const [choice, setChoice] = React.useState<ConflictChoice>("keep");
  const [each, setEach] = React.useState<ReadonlyMap<string, ConflictChoice>>(new Map());

  const finish = () => {
    onChoose(new Map(names.map((name) => [name, perItem ? each.get(name) ?? "keep" : choice])));
  };

  return (
    <Dialog open onOpenChange={(next) => !next && onCancel()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader className="pb-4">
          <DialogTitle>{names.length === 1 ? "A File Already Exists" : "Files Already Exist"}</DialogTitle>
          <DialogDescription>
            {names.length} of {total} {total === 1 ? "item" : "items"} already exist in {guestText(folder)}.
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          {perItem ? (
            <ul className="max-h-72 space-y-1 overflow-auto text-sm">
              {names.slice(0, SHOWN).map((name) => (
                <li key={name} className="flex items-center justify-between gap-3">
                  <OverflowText label="File name" value={guestText(name)} mono mode="overlay" className="min-w-0 text-xs" />
                  <select
                    aria-label={`Choice for ${guestText(name)}`}
                    className="h-8 rounded-md border bg-background px-2 text-xs"
                    value={each.get(name) ?? "keep"}
                    onChange={(event) => setEach(new Map(each).set(name, event.target.value as ConflictChoice))}
                  >
                    {CHOICES.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                  </select>
                </li>
              ))}
            </ul>
          ) : (
            <div role="radiogroup" aria-label="What to do" className="space-y-2">
              {CHOICES.map((option) => (
                <Label
                  key={option.value}
                  className={cn(
                    "flex cursor-pointer items-start gap-2 rounded-md border p-2 font-normal",
                    choice === option.value && "border-primary",
                  )}
                >
                  <input
                    type="radio"
                    name="instance-file-conflict"
                    className="mt-0.5"
                    checked={choice === option.value}
                    onChange={() => setChoice(option.value)}
                  />
                  <span>
                    <span className="block text-sm font-medium">{option.label}</span>
                    <span className="block text-xs text-muted-foreground">{option.description}</span>
                  </span>
                </Label>
              ))}
            </div>
          )}
        </DialogBody>
        <DialogFooter>
          {names.length > 1 && (
            <Button type="button" variant="ghost" className="sm:mr-auto" onClick={() => setPerItem(!perItem)}>
              {perItem ? "Choose for all" : "Choose for each"}
            </Button>
          )}
          <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
          <Button type="button" variant={!perItem && choice === "replace" ? "destructive" : "default"} onClick={finish}>
            Continue
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

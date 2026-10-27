"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { COMBO_KEYS, MODIFIERS, customCombo, type KeyCombo, type Modifier, type ScanKey } from "@/lib/spice/keys";

const MODIFIER_LABEL: Record<Modifier, string> = { ctrl: "Ctrl", alt: "Alt", shift: "Shift", win: "Win" };

function keyLabel(key: ScanKey): string {
  return key.length === 1 ? key.toUpperCase() : key[0].toUpperCase() + key.slice(1);
}

/** Build a combination the menu does not list: any of four modifiers and one key. */
export function CustomComboDialog({
  open,
  onOpenChange,
  onSend,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSend: (combo: KeyCombo) => void;
}) {
  const [modifiers, setModifiers] = useState<Modifier[]>(["ctrl", "alt"]);
  const [key, setKey] = useState<ScanKey | "">("");
  const combo = key ? customCombo(modifiers, key) : null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Send a Key Combination</DialogTitle>
          <DialogDescription>Pick the modifiers to hold and the key to press. They are released in reverse order.</DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Hold</legend>
            <div className="flex flex-wrap gap-4">
              {MODIFIERS.map((modifier) => (
                <div key={modifier} className="flex items-center gap-2">
                  <Checkbox
                    id={`combo-${modifier}`}
                    checked={modifiers.includes(modifier)}
                    onCheckedChange={(checked) =>
                      setModifiers((current) =>
                        checked ? [...current, modifier] : current.filter((m) => m !== modifier),
                      )
                    }
                  />
                  <Label htmlFor={`combo-${modifier}`}>{MODIFIER_LABEL[modifier]}</Label>
                </div>
              ))}
            </div>
          </fieldset>
          <div className="space-y-2">
            <Label htmlFor="combo-key">Key</Label>
            <Select value={key} onValueChange={(value) => setKey(value as ScanKey)}>
              <SelectTrigger id="combo-key" className="w-full">
                <SelectValue placeholder="Choose a key" />
              </SelectTrigger>
              <SelectContent>
                {COMBO_KEYS.map((k) => (
                  <SelectItem key={k} value={k}>
                    {keyLabel(k)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {combo ? <>Sends <span className="font-mono text-foreground">{combo.label}</span>.</> : "Choose a key to send."}
          </p>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={!combo}
            onClick={() => {
              if (!combo) return;
              onSend(combo);
              onOpenChange(false);
            }}
          >
            Send
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Clipboard Manager - Side panel for managing clipboard entries.
 * Supports adding text, viewing history, and inserting into console.
 *
 * important: history lives in memory for this page only. It used to be persisted to
 * localStorage, where pasted passwords and keys stayed in plain text after the tab closed; the
 * old key is removed on first use. Text the guest copies (through its agent) is added here too.
 */

"use client";

import * as React from "react";
import { useState, useCallback, useEffect } from "react";
import {
  X,
  Plus,
  Trash2,
  ClipboardPaste,
  Copy,
  Check,
  History,
  Send,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn, formatRelativeTime } from "@/lib/utils";
import { toast } from "@/hooks/use-toast";
import { copyText } from "@/lib/clipboard";

interface ClipboardEntry {
  id: string;
  text: string;
  createdAt: number;
}

interface ClipboardManagerProps {
  onInsert: (text: string) => void;
  onClose: () => void;
  className?: string;
}

const LEGACY_STORAGE_KEY = 'orcastra-clipboard-history';
const MAX_HISTORY = 50;

// One history for every console panel on the page, gone when the page goes.
let sessionHistory: ClipboardEntry[] = [];
const listeners = new Set<(entries: ClipboardEntry[]) => void>();

function publish(entries: ClipboardEntry[]): void {
  sessionHistory = entries.slice(0, MAX_HISTORY);
  listeners.forEach((listener) => listener(sessionHistory));
}

/** Adds text to the history (for example what the guest copied); a repeat moves to the top. */
export function rememberClipboard(text: string): void {
  const trimmed = text.trim();
  if (!trimmed) return;
  const entry: ClipboardEntry = { id: `clip_${Date.now()}_${Math.random().toString(36).slice(2, 11)}`, text: trimmed, createdAt: Date.now() };
  publish([entry, ...sessionHistory.filter((e) => e.text !== trimmed)]);
}

export function ClipboardManager({ onInsert, onClose, className }: ClipboardManagerProps) {
  const [entries, setEntries] = useState<ClipboardEntry[]>(sessionHistory);
  const [newText, setNewText] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  useEffect(() => {
    try {
      localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch {
      // storage unavailable; nothing to clean
    }
    listeners.add(setEntries);
    return () => {
      listeners.delete(setEntries);
    };
  }, []);

  const saveHistory = useCallback((newEntries: ClipboardEntry[]) => {
    publish(newEntries);
  }, []);

  // Add new entry
  const handleAdd = useCallback(() => {
    if (!newText.trim()) {
      toast({
        title: "Empty text",
        description: "Enter some text to add.",
        variant: "destructive",
      });
      return;
    }

    const entry: ClipboardEntry = {
      id: `clip_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
      text: newText.trim(),
      createdAt: Date.now(),
    };

    const newEntries = [entry, ...entries].slice(0, MAX_HISTORY);
    setEntries(newEntries);
    saveHistory(newEntries);
    setNewText('');
    setShowAddForm(false);
    setSelectedId(entry.id);

    toast({
      title: "Text added to clipboard",
      description: "Text added to clipboard history.",
    });
  }, [newText, entries, saveHistory]);

  // Paste from system clipboard
  const handlePasteFromSystem = useCallback(async () => {
    // Check if clipboard API is available (requires HTTPS or localhost)
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      // Fallback: prompt user to paste manually
      setShowAddForm(true);
      toast({
        title: "Paste manually",
        description: "Use Ctrl+V to paste text into the text area. Clipboard API requires HTTPS.",
      });
      return;
    }
    
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        setNewText(text);
        setShowAddForm(true);
        toast({
          title: "Text pasted from clipboard",
          description: "Text added to the form. Click Add to save.",
        });
      } else {
        toast({
          title: "Clipboard empty",
          description: "No text found in system clipboard.",
          variant: "destructive",
        });
      }
    } catch (e) {
      // Fallback for permission denied or other errors
      setShowAddForm(true);
      toast({
        title: "Paste manually",
        description: "Use Ctrl+V to paste text into the text area below.",
      });
    }
  }, []);

  // Delete entry
  const handleDelete = useCallback((id: string) => {
    const newEntries = entries.filter(e => e.id !== id);
    setEntries(newEntries);
    saveHistory(newEntries);
    if (selectedId === id) {
      setSelectedId(null);
    }
  }, [entries, selectedId, saveHistory]);

  // Clear all entries
  const handleClearAll = useCallback(() => {
    setEntries([]);
    saveHistory([]);
    setSelectedId(null);
    toast({
      title: "Clipboard cleared",
      description: "All clipboard entries cleared.",
    });
  }, [saveHistory]);

  // Copy entry text to system clipboard
  const handleCopy = useCallback(async (entry: ClipboardEntry) => {
    const ok = await copyText(entry.text);
    if (!ok) {
      toast({
        title: "Could not copy text",
        description: "Unable to copy to clipboard.",
        variant: "destructive",
      });
      return;
    }
    setCopiedId(entry.id);
    setTimeout(() => setCopiedId(null), 2000);
  }, []);

  // Insert selected entry into console
  const handleInsert = useCallback(() => {
    if (!selectedId) {
      toast({
        title: "No clipboard entry selected",
        description: "Select a clipboard entry first.",
        variant: "destructive",
      });
      return;
    }

    const entry = entries.find(e => e.id === selectedId);
    if (entry) {
      onInsert(entry.text);
      toast({
        title: "Text inserted",
        description: "Text sent to console.",
      });
    }
  }, [selectedId, entries, onInsert]);

  return (
    <TooltipProvider>
      <div 
        className={cn(
          "flex flex-col h-full bg-background border-l shadow-lg",
          "w-80 max-w-[90vw]",
          className
        )}
        onMouseDown={(e) => e.stopPropagation()}
        onTouchStart={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-3 border-b bg-muted/50">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm">Clipboard Manager</span>
            <span className="text-xs text-muted-foreground">({entries.length})</span>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </Button>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1 p-2 border-b">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-8 gap-1.5 flex-1"
                onClick={() => setShowAddForm(true)}
              >
                <Plus className="h-3.5 w-3.5" />
                <span className="text-xs">New</span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>Add new text</TooltipContent>
          </Tooltip>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-8 gap-1.5 flex-1"
                onClick={handlePasteFromSystem}
              >
                <ClipboardPaste className="h-3.5 w-3.5" />
                <span className="text-xs">Paste</span>
              </Button>
            </TooltipTrigger>
            <TooltipContent>Paste from system clipboard</TooltipContent>
          </Tooltip>

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-8"
                onClick={handleClearAll}
                disabled={entries.length === 0}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Clear all</TooltipContent>
          </Tooltip>
        </div>

        {/* Add form */}
        {showAddForm && (
          <div className="p-2 border-b space-y-2">
            <Textarea
              value={newText}
              onChange={(e) => setNewText(e.target.value)}
              placeholder="Enter or paste text here…"
              className="min-h-[80px] text-sm resize-none"
              autoFocus
            />
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                className="flex-1 h-8"
                onClick={handleAdd}
              >
                <Plus className="h-3.5 w-3.5 mr-1" />
                Add
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="h-8"
                onClick={() => { setShowAddForm(false); setNewText(''); }}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        {/* History list */}
        <ScrollArea className="flex-1">
          {entries.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-40 text-muted-foreground">
              <History className="h-8 w-8 mb-2 opacity-50" />
              <p className="text-sm">No clipboard history</p>
              <p className="text-xs">Add text or paste from clipboard</p>
            </div>
          ) : (
            <div className="p-2 space-y-1">
              {entries.map((entry) => (
                <div
                  key={entry.id}
                  onClick={() => setSelectedId(entry.id)}
                  className={cn(
                    "group relative p-2 rounded-md cursor-pointer transition-colors",
                    "hover:bg-accent/50 border",
                    selectedId === entry.id 
                      ? "bg-accent border-primary" 
                      : "border-transparent"
                  )}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-mono line-clamp-2 break-all">
                        {entry.text}
                      </p>
                      <p className="text-xs text-muted-foreground mt-1">
                        {formatRelativeTime(entry.createdAt)}
                      </p>
                    </div>
                    <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-6 w-6"
                        onClick={(e) => { e.stopPropagation(); handleCopy(entry); }}
                      >
                        {copiedId === entry.id ? (
                          <Check className="h-3 w-3 text-status-online" />
                        ) : (
                          <Copy className="h-3 w-3" />
                        )}
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-6 w-6 text-destructive"
                        onClick={(e) => { e.stopPropagation(); handleDelete(entry.id); }}
                      >
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </ScrollArea>

        {/* Insert button */}
        <div className="p-2 border-t bg-muted/30">
          <Button
            className="w-full"
            onClick={handleInsert}
            disabled={!selectedId}
          >
            Insert to Console
          </Button>
        </div>
      </div>
    </TooltipProvider>
  );
}

export default ClipboardManager;

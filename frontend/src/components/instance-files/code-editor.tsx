"use client";

import * as React from "react";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { LanguageDescription, bracketMatching, defaultHighlightStyle, foldGutter, indentOnInput, syntaxHighlighting } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { highlightSelectionMatches, search, searchKeymap } from "@codemirror/search";
import { Compartment, EditorState } from "@codemirror/state";
import {
  EditorView,
  drawSelection,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
} from "@codemirror/view";

export interface CodeEditorHandle {
  focus: () => void;
  value: () => string;
}

interface CodeEditorProps {
  fileName: string;
  initial: string;
  readOnly: boolean;
  onChange: (text: string) => void;
  onSave: () => void;
  ariaLabel: string;
}

// important: a guest's file can hide what it says. Beyond the control characters CodeMirror marks
// by default, bidirectional overrides are shown, because they are how source reads as one thing
// and runs as another.
const HIDDEN = new RegExp("[\\u0000-\\u0008\\u000a-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u2069\\ufeff\\ufff9-\\ufffc]", "gu");

const theme = EditorView.theme({
  "&": { height: "100%", fontSize: "12px", backgroundColor: "hsl(var(--background))", color: "hsl(var(--foreground))" },
  ".cm-scroller": { fontFamily: "var(--font-geist-mono), ui-monospace, monospace", overflow: "auto" },
  ".cm-gutters": { backgroundColor: "hsl(var(--muted))", color: "hsl(var(--muted-foreground))", border: "none" },
  ".cm-activeLine": { backgroundColor: "hsl(var(--muted) / 0.4)" },
  ".cm-activeLineGutter": { backgroundColor: "hsl(var(--muted))" },
  ".cm-cursor": { borderLeftColor: "hsl(var(--foreground))" },
  ".cm-panels": { backgroundColor: "hsl(var(--popover))", color: "hsl(var(--popover-foreground))" },
  "&.cm-focused": { outline: "none" },
});

function lineEndingOf(text: string): string {
  return text.includes("\r\n") ? "\r\n" : "\n";
}

/** CodeMirror 6. Loaded on its own only when a person opens a file, so no other page pays for it. */
const CodeEditor = React.forwardRef<CodeEditorHandle, CodeEditorProps>(function CodeEditor(
  { fileName, initial, readOnly, onChange, onSave, ariaLabel },
  ref,
) {
  const hostRef = React.useRef<HTMLDivElement>(null);
  const viewRef = React.useRef<EditorView | null>(null);
  const callbacks = React.useRef({ onChange, onSave });
  callbacks.current = { onChange, onSave };

  React.useImperativeHandle(ref, () => ({
    focus: () => viewRef.current?.focus(),
    value: () => viewRef.current?.state.doc.toString() ?? initial,
  }), [initial]);

  React.useEffect(() => {
    if (!hostRef.current) return;
    const language = new Compartment();
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: initial,
        extensions: [
          // A saved file keeps the line endings it came with.
          EditorState.lineSeparator.of(lineEndingOf(initial)),
          lineNumbers(),
          foldGutter(),
          history(),
          drawSelection(),
          highlightSpecialChars({ addSpecialChars: HIDDEN }),
          highlightActiveLine(),
          highlightActiveLineGutter(),
          highlightSelectionMatches(),
          bracketMatching(),
          indentOnInput(),
          search({ top: true }),
          syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
          language.of([]),
          EditorState.readOnly.of(readOnly),
          EditorView.editable.of(!readOnly),
          EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
          theme,
          keymap.of([
            { key: "Mod-s", preventDefault: true, run: () => { callbacks.current.onSave(); return true; } },
            ...defaultKeymap,
            ...historyKeymap,
            ...searchKeymap,
            indentWithTab,
          ]),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) callbacks.current.onChange(update.state.doc.toString());
          }),
        ],
      }),
    });
    viewRef.current = view;
    // important: Tab indents here, so without this a keyboard user cannot leave the editor. Escape
    // then Tab moves on, which is CodeMirror's documented way out.
    view.setTabFocusMode(false);
    const match = LanguageDescription.matchFilename(languages, fileName);
    let disposed = false;
    match?.load().then(
      (support) => {
        if (!disposed) view.dispatch({ effects: language.reconfigure(support) });
      },
      () => undefined,
    );
    return () => {
      disposed = true;
      view.destroy();
      viewRef.current = null;
    };
    // The document is created once per opening: `initial` changing mid-edit would discard work.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileName, readOnly]);

  return <div ref={hostRef} className="h-full min-h-0 overflow-hidden" />;
});

export default CodeEditor;

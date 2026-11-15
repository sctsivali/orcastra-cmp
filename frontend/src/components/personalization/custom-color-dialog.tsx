"use client";

import * as React from "react";
import Image from "next/image";
import { Check, Monitor, Palette, Settings, Type } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import {
  accentForeground,
  isHexColor,
  mutedForText,
  relativeLuminance,
  resolvedSidebarText,
  sameColor,
} from "@/lib/personalization-colors";
import {
  COLOR_PRESETS,
  DEFAULT_PERSONALIZATION,
  SIDEBAR_BG_PRESETS,
  type ColorSettings,
  type PersonalizationSettings,
} from "@/types/personalization";

const DEFAULTS = DEFAULT_PERSONALIZATION.colors;

const SIDEBAR_TEXT_PRESETS = [
  { name: "Dark", value: "#1e293b" },
  { name: "Black", value: "#0f172a" },
  { name: "Slate", value: "#334155" },
  { name: "Gray", value: "#4b5563" },
  { name: "White (Default)", value: "#f8fafc" },
  { name: "Light", value: "#e2e8f0" },
  { name: "Indigo", value: "#e0e7ff" },
  { name: "Cyan", value: "#cffafe" },
];

const SELECTED_RING = "border-foreground ring-2 ring-offset-2 ring-offset-background ring-foreground/20";

function isLight(hex: string): boolean {
  return (relativeLuminance(hex) ?? 0) > 0.5;
}

interface SwatchProps {
  name: string;
  value: string;
  selected: boolean;
  onSelect: () => void;
  children?: React.ReactNode;
  background?: string;
  /** Marks the accent choices, so the dialog can open on the selected one. */
  accentChoice?: boolean;
}

/** One preset colour. aria-pressed carries the selection, so it is never colour alone. */
function Swatch({ name, value, selected, onSelect, children, background, accentChoice }: SwatchProps) {
  return (
    <button
      type="button"
      aria-label={name}
      aria-pressed={selected}
      title={name}
      data-accent-choice={accentChoice ? "" : undefined}
      className={cn(
        "flex h-8 w-8 items-center justify-center rounded-md border-2 transition-transform hover:scale-110 motion-reduce:transition-none motion-reduce:hover:scale-100",
        selected ? SELECTED_RING : isLight(background ?? value) ? "border-muted-foreground/30" : "border-muted",
      )}
      style={{ backgroundColor: background ?? value }}
      onClick={onSelect}
    >
      {children}
    </button>
  );
}

interface HexColorFieldProps {
  label: string;
  value: string;
  fallback: string;
  selected?: boolean;
  onCommit: (hex: string) => void;
  inputRef?: React.Ref<HTMLInputElement>;
}

/**
 * The picker and the typed hex for one colour. A typed value is committed only once it is a full
 * #RRGGBB: the settings, the live sidebar and the save all take whole colours, and a partial one
 * used to paint the sidebar pill transparent and come back from the server as a 422.
 */
function HexColorField({ label, value, fallback, selected = false, onCommit, inputRef }: HexColorFieldProps) {
  const shown = isHexColor(value) ? value : fallback;
  const [draft, setDraft] = React.useState(shown);
  React.useEffect(() => setDraft(shown), [shown]);
  const id = React.useId();
  const incomplete = !isHexColor(draft);

  return (
    <div className="flex items-center gap-3">
      <Label htmlFor={id} className="text-xs text-muted-foreground">
        Custom
      </Label>
      <input
        type="color"
        aria-label={`Pick ${label}`}
        value={shown}
        onChange={(e) => onCommit(e.target.value)}
        className={cn("h-8 w-10 cursor-pointer rounded border-0 p-0", selected && "ring-2 ring-foreground ring-offset-2 ring-offset-background")}
      />
      <Input
        id={id}
        ref={inputRef}
        name={`${label.toLowerCase().replace(/\s+/g, "-")}-hex`}
        aria-label={`${label} hex value`}
        aria-invalid={incomplete || undefined}
        data-selected={selected ? "" : undefined}
        autoComplete="off"
        spellCheck={false}
        value={draft}
        onChange={(e) => {
          const next = e.target.value;
          if (!/^#?[0-9A-Fa-f]{0,6}$/.test(next)) return;
          const withHash = next.startsWith("#") ? next : `#${next}`;
          setDraft(withHash);
          if (isHexColor(withHash)) onCommit(withHash);
        }}
        onBlur={() => {
          if (incomplete) setDraft(shown);
        }}
        placeholder={`${fallback}…`}
        className={cn("w-28 font-mono text-sm", selected && "border-foreground")}
        maxLength={7}
      />
      {selected ? <span className="text-xs text-muted-foreground">Selected</span> : null}
    </div>
  );
}

/** The sidebar as the app paints it: ground, resolved text, and the active item on the accent. */
function SidebarPreview({ colors }: { colors: ColorSettings }) {
  const accent = isHexColor(colors.accentColor) ? colors.accentColor : DEFAULTS.accentColor;
  const custom = colors.useCustomSidebarBg;
  const ground = isHexColor(colors.sidebarBgColor) ? colors.sidebarBgColor : DEFAULTS.sidebarBgColor;
  const text = resolvedSidebarText(colors);
  const onAccent = accentForeground(accent);

  return (
    <div className="rounded-lg border p-3">
      <p className="mb-2 text-xs text-muted-foreground">Live Preview</p>
      <div
        className={cn("space-y-1 rounded-md p-3", !custom && "border bg-card text-foreground")}
        style={custom ? { backgroundColor: ground, color: text } : undefined}
      >
        <div className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium" style={{ backgroundColor: accent, color: onAccent }}>
          <Settings className="h-4 w-4" aria-hidden="true" />
          Settings
        </div>
        <div className="flex items-center gap-2 px-2 py-1.5 text-sm">
          <Palette className="h-4 w-4" aria-hidden="true" />
          <span>Personalization</span>
          <span className="ml-auto text-xs" style={custom ? { color: mutedForText(text) } : undefined}>
            inactive item
          </span>
        </div>
      </div>
    </div>
  );
}

interface CustomColorDialogProps {
  settings: PersonalizationSettings;
  updateColors: (colors: Partial<ColorSettings>) => void;
}

/** "Customize Colors": the accent, the sidebar ground, the logo variant and the sidebar text. */
export function CustomColorDialog({ settings, updateColors }: CustomColorDialogProps) {
  const [open, setOpen] = React.useState(false);
  const contentRef = React.useRef<HTMLDivElement>(null);
  const customAccentRef = React.useRef<HTMLInputElement>(null);
  const colors = settings.colors;
  const accent = colors.accentColor || DEFAULTS.accentColor;
  const accentIsPreset = COLOR_PRESETS.some((preset) => sameColor(accent, preset.value));
  const groundIsPreset = SIDEBAR_BG_PRESETS.some((preset) => sameColor(colors.sidebarBgColor, preset.value));
  const autoText = colors.autoTextColor !== false && !colors.useCustomSidebarText;
  const textIsPreset = SIDEBAR_TEXT_PRESETS.some((preset) => sameColor(colors.sidebarTextColor, preset.value));
  const logoPref = colors.sidebarLogoColor || "auto";
  const hasCustomLogo = !!(settings.logo.lightModeLogo || settings.logo.darkModeLogo);

  return (
    <>
      <Button variant="outline" className="w-full gap-2" onClick={() => setOpen(true)}>
        <Palette className="h-4 w-4" aria-hidden="true" />
        Customize Colors
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          ref={contentRef}
          className="max-h-[85vh] max-w-2xl overflow-y-auto overscroll-contain"
          // important: open on the selected accent, not on the first preset. Radix focuses the first
          // tabbable element, and the focus ring (drawn in the accent) around the purple preset read
          // as "purple is selected" while a custom accent was in use.
          onOpenAutoFocus={(event) => {
            const selected =
              contentRef.current?.querySelector<HTMLElement>('[data-accent-choice][aria-pressed="true"]') ??
              customAccentRef.current;
            if (selected) {
              event.preventDefault();
              selected.focus();
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>Custom Color Settings</DialogTitle>
            <DialogDescription>Fine-tune individual color settings for your dashboard</DialogDescription>
          </DialogHeader>

          <div className="space-y-6 py-4">
            <section className="space-y-3" aria-labelledby="accent-color-heading">
              <div>
                <h4 id="accent-color-heading" className="text-sm font-medium">Accent Color</h4>
                <p className="text-xs text-muted-foreground">Buttons and active menu items</p>
              </div>
              <div className="space-y-2">
                <p className="text-xs text-muted-foreground">Presets</p>
                <div className="flex flex-wrap gap-2">
                  {COLOR_PRESETS.map((preset) => (
                    <Swatch
                      key={preset.value}
                      accentChoice
                      name={preset.name}
                      value={preset.value}
                      selected={sameColor(accent, preset.value)}
                      onSelect={() => updateColors({ accentColor: preset.value, useCustomAccent: false })}
                    />
                  ))}
                </div>
              </div>
              <HexColorField
                label="Accent color"
                value={accent}
                fallback={DEFAULTS.accentColor}
                selected={!accentIsPreset}
                inputRef={customAccentRef}
                onCommit={(hex) => updateColors({ accentColor: hex, useCustomAccent: true })}
              />
            </section>

            <section className="space-y-3 border-t pt-6" aria-labelledby="sidebar-ground-heading">
              <div>
                <h4 id="sidebar-ground-heading" className="text-sm font-medium">Sidebar Background</h4>
                <p className="text-xs text-muted-foreground">
                  {!colors.useCustomSidebarBg ? "Using system theme (follows dark/light mode)" : "Custom sidebar background color"}
                </p>
              </div>
              <Button
                variant={!colors.useCustomSidebarBg ? "default" : "outline"}
                size="sm"
                aria-pressed={!colors.useCustomSidebarBg}
                onClick={() => updateColors({ useCustomSidebarBg: false, autoTextColor: true })}
                className="gap-2"
              >
                <Monitor className="h-4 w-4" aria-hidden="true" />
                Use System Theme
              </Button>
              <div className="space-y-2">
                <p className="text-xs text-muted-foreground">Or choose a custom color</p>
                <div className="flex flex-wrap gap-2">
                  {SIDEBAR_BG_PRESETS.map((preset) => (
                    <Swatch
                      key={preset.value}
                      name={preset.name}
                      value={preset.value}
                      selected={colors.useCustomSidebarBg && sameColor(colors.sidebarBgColor, preset.value)}
                      onSelect={() => updateColors({ sidebarBgColor: preset.value, useCustomSidebarBg: true, autoTextColor: true })}
                    />
                  ))}
                </div>
              </div>
              <HexColorField
                label="Sidebar background"
                value={colors.sidebarBgColor}
                fallback={DEFAULTS.sidebarBgColor}
                selected={colors.useCustomSidebarBg && !groundIsPreset}
                onCommit={(hex) => updateColors({ sidebarBgColor: hex, useCustomSidebarBg: true, autoTextColor: true })}
              />
            </section>

            <section className="space-y-3 border-t pt-6" aria-labelledby="sidebar-logo-heading">
              <div className="flex items-center justify-between">
                <div>
                  <h4 id="sidebar-logo-heading" className="text-sm font-medium">Sidebar Logo Color</h4>
                  <p className="text-xs text-muted-foreground">
                    {hasCustomLogo ? "Disabled when using custom logo" : "Choose logo color variant for the sidebar"}
                  </p>
                </div>
                <div className="flex gap-1">
                  <div className={cn("flex h-6 w-6 items-center justify-center rounded border bg-white", hasCustomLogo && "opacity-50")}>
                    <Image src="/orca-logo-black.png" alt="Black logo" width={20} height={20} className="object-contain" />
                  </div>
                  <div className={cn("flex h-6 w-6 items-center justify-center rounded border border-slate-700 bg-slate-800", hasCustomLogo && "opacity-50")}>
                    <Image src="/orca-logo-white.png" alt="White logo" width={20} height={20} className="object-contain" />
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-3">
                {(["auto", "black", "white"] as const).map((choice) => (
                  <Button
                    key={choice}
                    variant={logoPref === choice ? "default" : "outline"}
                    size="sm"
                    aria-pressed={logoPref === choice}
                    onClick={() => updateColors({ sidebarLogoColor: choice })}
                    className="gap-2 capitalize"
                    disabled={hasCustomLogo}
                  >
                    <Check className={cn("h-4 w-4", logoPref === choice ? "opacity-100" : "opacity-0")} aria-hidden="true" />
                    {choice}
                  </Button>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                {hasCustomLogo
                  ? "Remove custom logos to use default logo color variants"
                  : "Auto: follows sidebar background brightness (or system theme when using default sidebar)"}
              </p>
            </section>

            <section className="space-y-3 border-t pt-6" aria-labelledby="sidebar-text-heading">
              <div>
                <h4 id="sidebar-text-heading" className="text-sm font-medium">Sidebar Text Color</h4>
                <p className="text-xs text-muted-foreground">
                  {autoText ? "Auto-calculated based on background brightness" : "Custom text color for sidebar labels and icons"}
                </p>
              </div>
              <div className="flex items-center gap-4">
                <Button
                  variant={autoText ? "default" : "outline"}
                  size="sm"
                  aria-pressed={autoText}
                  onClick={() => updateColors({ autoTextColor: true, useCustomSidebarText: false })}
                  className="gap-2"
                >
                  <Check className={cn("h-4 w-4", autoText ? "opacity-100" : "opacity-0")} aria-hidden="true" />
                  Auto
                </Button>
                <Button
                  variant={colors.useCustomSidebarText ? "default" : "outline"}
                  size="sm"
                  aria-pressed={!!colors.useCustomSidebarText}
                  onClick={() => updateColors({ useCustomSidebarText: true, autoTextColor: false })}
                  className="gap-2"
                >
                  <Check className={cn("h-4 w-4", colors.useCustomSidebarText ? "opacity-100" : "opacity-0")} aria-hidden="true" />
                  Manual
                </Button>
              </div>
              {colors.useCustomSidebarText && (
                <>
                  <div className="space-y-2">
                    <p className="text-xs text-muted-foreground">Presets</p>
                    <div className="flex flex-wrap gap-2">
                      {SIDEBAR_TEXT_PRESETS.map((preset) => (
                        <Swatch
                          key={preset.value}
                          name={preset.name}
                          value={preset.value}
                          background={isLight(preset.value) ? "#1e293b" : "#f8fafc"}
                          selected={sameColor(colors.sidebarTextColor, preset.value)}
                          onSelect={() => updateColors({ sidebarTextColor: preset.value })}
                        >
                          <Type className="h-4 w-4" style={{ color: preset.value }} aria-hidden="true" />
                        </Swatch>
                      ))}
                    </div>
                  </div>
                  <HexColorField
                    label="Sidebar text"
                    value={colors.sidebarTextColor}
                    fallback={DEFAULTS.sidebarTextColor}
                    selected={!textIsPreset}
                    onCommit={(hex) => updateColors({ sidebarTextColor: hex, useCustomSidebarText: true, autoTextColor: false })}
                  />
                </>
              )}
            </section>

            <div className="border-t pt-6">
              <SidebarPreview colors={colors} />
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

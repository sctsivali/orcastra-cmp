"use client";

import * as React from "react";
import { AlertTriangle, Check, ChevronDown, RefreshCw } from "lucide-react";

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ProjectChipList } from "@/components/integrations/project-chip-list";
import { cn, plural } from "@/lib/utils";
import type { ProjectScopeOption } from "@/hooks/use-project-scope-options";

// Matches SearchableSelect and ClusterAccessSelector: short lists stay uncluttered.
const SEARCH_THRESHOLD = 6;

export interface ProjectScopePickerProps {
  /**
   * Ties the trigger to its Label.
   *
   * note: a button is a labelable element, so `<Label htmlFor={id}>` gives it its accessible
   * name and getByLabel resolves it. Same device as the selects above it in the dialog.
   */
  id: string;
  /** The picked names, exactly as they will be sent. Owned by the caller, never mutated here. */
  value: readonly string[];
  /** Full replacement, never a delta. */
  onChange: (next: string[]) => void;
  /** Cluster ids ticked below. Zero of them is a real state this control has to explain. */
  selectedClusterIds: readonly string[];
  options: readonly ProjectScopeOption[];
  answeredCount: number;
  unansweredClusterLabels: readonly string[];
  orphanedProjects: readonly string[];
  partiallyCoveredProjects: readonly ProjectScopeOption[];
  isLoading: boolean;
  isError: boolean;
  onRetry: () => Promise<void>;
  /** Locked while a create is in flight, so a pick cannot change under an open request. */
  disabled?: boolean;
}

function listSentence(labels: readonly string[]): string {
  if (labels.length === 1) return labels[0];
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

/**
 * Picks the projects an API key grant names, from the projects the selected clusters actually
 * have.
 *
 * This replaced a comma-separated text box. A name is compared literally at every enforcement
 * site, so one typo produced a key that authenticated, read as correctly scoped on its card, and
 * was refused by every project-bounded call for the rest of its life, silently. Typing here only
 * searches. The value is always a click on something the cluster reported.
 */
export function ProjectScopePicker({
  id,
  value,
  onChange,
  selectedClusterIds,
  options,
  answeredCount,
  unansweredClusterLabels,
  orphanedProjects,
  partiallyCoveredProjects,
  isLoading,
  isError,
  onRetry,
  disabled = false,
}: ProjectScopePickerProps) {
  const [open, setOpen] = React.useState(false);
  const [retrying, setRetrying] = React.useState(false);
  const [manualEntry, setManualEntry] = React.useState("");
  const listboxId = React.useId();
  const triggerRef = React.useRef<HTMLButtonElement>(null);

  const hasClusters = selectedClusterIds.length > 0;
  const isBlocked = disabled || !hasClusters;
  // The fallback exists only for the case the picker genuinely cannot cover. Offering it while
  // the list loaded would reopen the typo door this control was built to close.
  const degraded = unansweredClusterLabels.length > 0 || isError;

  // important: freeze the rendered rows while the popover is open. The projects read polls every
  // five minutes, and re-sorting a live list moves a row out from under the cursor between the
  // eye and the click. On a control that writes a permission grant that is the wrong project,
  // not a cosmetic glitch. Same device as SearchableSelect.
  //
  // important: the empty list is exempt, and that exemption is load-bearing. This read is a live
  // fan-out across every cluster in scope, so on a real fleet it lands seconds after the popover
  // opens. Freezing the empty array captured at open time left the panel reading "No projects
  // visible on the selected clusters." for as long as it stayed open, which says the clusters
  // have nothing when the truth is that the answer had not arrived. Nothing can move under the
  // cursor while nothing is rendered, so adopting the first non-empty list costs the guarantee
  // nothing. Found on the live fleet; the fixture specs answer instantly and never saw it.
  const frozen = React.useRef(options);
  if (!open || frozen.current.length === 0) frozen.current = options;
  const rendered = open ? frozen.current : options;

  const toggle = (name: string) => {
    onChange(
      value.includes(name) ? value.filter((entry) => entry !== name) : [...value, name],
    );
  };

  const addManual = () => {
    const name = manualEntry.trim();
    if (!name || value.includes(name)) {
      setManualEntry("");
      return;
    }
    onChange([...value, name]);
    setManualEntry("");
  };

  const handleRetry = async () => {
    setRetrying(true);
    try {
      await onRetry();
    } catch {
      // The notice is already on screen and says the same thing. Nothing to add.
    } finally {
      setRetrying(false);
    }
  };

  let triggerLabel: string;
  if (!hasClusters) triggerLabel = "Select clusters first";
  else if (value.length === 0) triggerLabel = "Select projects";
  else triggerLabel = `${plural(value.length, "project")} selected`;

  let emptyText: string;
  if (isLoading) emptyText = "Loading projects…";
  else if (isError) emptyText = "Projects could not be loaded.";
  // "visible" is load-bearing: the projects read applies the tenant ACL, so an empty list can
  // mean the operator may not see them rather than the cluster not having any.
  else if (rendered.length === 0) emptyText = "No projects visible on the selected clusters.";
  else emptyText = "No project matches that search.";

  const showCoverage = answeredCount > 1;

  return (
    <div className="space-y-2" data-slot="project-scope-picker">
      <Label htmlFor={id}>Projects</Label>
      <Popover open={open} onOpenChange={setOpen} modal={true}>
        <PopoverTrigger asChild>
          <button
            ref={triggerRef}
            id={id}
            type="button"
            role="combobox"
            aria-expanded={open}
            aria-controls={listboxId}
            disabled={isBlocked}
            onKeyDown={(event) => {
              // A combobox opens on ArrowDown. PopoverTrigger only opens on click, Enter and
              // Space, so without this a keyboard user arrowing down the form walks past it.
              if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                setOpen(true);
              }
            }}
            className="flex h-10 w-full items-center justify-between rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <span
              className={cn(
                "text-left",
                value.length > 0 ? "font-medium" : "text-muted-foreground",
              )}
            >
              {triggerLabel}
            </span>
            <ChevronDown aria-hidden="true" className="h-4 w-4 shrink-0 opacity-50" />
          </button>
        </PopoverTrigger>
        {/* important: the id goes on the panel, never on CommandList. cmdk sets its own id after
            spreading the caller's props, so an id passed there is dropped and this combobox's
            aria-controls would reference nothing. */}
        <PopoverContent
          id={listboxId}
          align="start"
          className="w-[var(--radix-popover-trigger-width)] p-0"
        >
          <Command>
            {rendered.length > SEARCH_THRESHOLD && (
              <CommandInput placeholder="Search projects" />
            )}
            <CommandList>
              <CommandEmpty>{emptyText}</CommandEmpty>
              <CommandGroup>
                {rendered.map((option) => {
                  const isSelected = value.includes(option.name);
                  return (
                    <CommandItem
                      key={option.name}
                      // The value stays the exact project name, so cmdk's own normalisation
                      // never becomes a second spelling of a string the backend matches
                      // literally. Everything else searchable goes in keywords.
                      value={option.name}
                      keywords={[option.description ?? ""]}
                      data-project={option.name}
                      aria-selected={isSelected}
                      // important: a closure, never onSelect's argument. cmdk derives that from
                      // its own normalised value, and Staging and staging are different grants.
                      //
                      // important: no setOpen(false). Multi-select means several toggles per
                      // visit, and closing on select is the obvious mistake here.
                      onSelect={() => toggle(option.name)}
                    >
                      {/* The check and the weight carry the state. --primary is the
                          user-configurable accent and is not contrast-clamped, so it can never
                          be the only signal. */}
                      <Check
                        aria-hidden="true"
                        className={cn(
                          "mr-2 h-4 w-4 shrink-0",
                          isSelected ? "opacity-100" : "opacity-0",
                        )}
                      />
                      <div className="flex min-w-0 flex-1 items-start justify-between gap-2">
                        <div className="min-w-0">
                          {/* break-all, not truncate: the name is the value being granted. */}
                          <p className={cn("break-all", isSelected && "font-medium")}>
                            {option.name}
                          </p>
                          {option.description && (
                            <p className="break-words text-xs text-muted-foreground">
                              {option.description}
                            </p>
                          )}
                        </div>
                        {showCoverage && (
                          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                            <span className="sr-only">Present on </span>
                            {option.presentOn.length} of {plural(answeredCount, "cluster")}
                          </span>
                        )}
                      </div>
                    </CommandItem>
                  );
                })}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>

      <ProjectChipList
        projects={value}
        orphaned={orphanedProjects}
        onRemove={(name) => onChange(value.filter((entry) => entry !== name))}
        onClearAll={() => onChange([])}
        returnFocusRef={triggerRef}
        disabled={disabled}
      />

      <p className="text-xs text-muted-foreground">
        {!hasClusters
          ? "Select at least one cluster to choose projects."
          : value.length === 0
            ? "Pick at least one project. The same projects apply to every selected cluster."
            : "The same projects apply to every selected cluster."}
      </p>

      {degraded && (
        <div role="status" className="flex items-start gap-2 text-xs">
          <AlertTriangle
            aria-hidden="true"
            className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-offline"
          />
          <p className="text-muted-foreground">
            {isError
              ? "Projects could not be loaded."
              : `${listSentence(unansweredClusterLabels)} did not answer, so ${
                  unansweredClusterLabels.length === 1 ? "its" : "their"
                } projects are not listed. Type a name below to grant it anyway.`}
            <button
              type="button"
              onClick={handleRetry}
              disabled={retrying}
              className="ml-1.5 inline-flex items-center gap-1 underline decoration-dotted underline-offset-2 hover:decoration-solid disabled:opacity-50"
            >
              <RefreshCw aria-hidden="true" className={cn("h-3 w-3", retrying && "animate-spin")} />
              {retrying ? "Retrying" : "Retry"}
            </button>
          </p>
        </div>
      )}

      {degraded && hasClusters && (
        <div className="space-y-1">
          <Label htmlFor={`${id}-manual`} className="text-xs font-normal text-muted-foreground">
            Add a project by name
          </Label>
          <Input
            id={`${id}-manual`}
            value={manualEntry}
            disabled={disabled}
            placeholder="Press Enter to add"
            onChange={(event) => setManualEntry(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              // The dialog's own submit would otherwise fire from inside this field.
              event.preventDefault();
              addManual();
            }}
          />
        </div>
      )}

      {partiallyCoveredProjects.length > 0 && (
        <div role="status" className="flex items-start gap-2 text-xs">
          <AlertTriangle
            aria-hidden="true"
            className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-offline"
          />
          <p className="text-muted-foreground">
            {partiallyCoveredProjects.length === 1
              ? `${partiallyCoveredProjects[0].name} is not on ${listSentence(
                  partiallyCoveredProjects[0].missingFromLabels,
                )}, so the grant there will match nothing.`
              : `${partiallyCoveredProjects.length} selected projects are missing from at least one selected cluster, so those grants will match nothing.`}
          </p>
        </div>
      )}

      {orphanedProjects.length > 0 && (
        <div role="status" className="flex items-start gap-2 text-xs">
          <AlertTriangle
            aria-hidden="true"
            className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive"
          />
          <p className="text-destructive">
            {orphanedProjects.length === 1
              ? `${orphanedProjects[0]} is not on any selected cluster. Remove it or select a cluster that has it.`
              : `${orphanedProjects.length} selected projects are not on any selected cluster. Remove them or select the clusters that have them.`}
          </p>
        </div>
      )}
    </div>
  );
}

"use client";

import * as React from "react";
import { X, Plus, Tag as TagIcon, ChevronDown, Loader2, Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn, plural } from "@/lib/utils";
import { toast } from "@/hooks/use-toast";
import { TagBadge, TagSwatch, TagToggle } from "./tag-chips";
import {
  TagGrouped,
  TAG_CATEGORY_PREFIXES,
  getTagDisplayName,
  getTagColor,
  groupTags,
} from "@/types/api";

// TagBadge lives in tag-chips.tsx; re-exported so existing imports from here keep working.
export { TagBadge } from "./tag-chips";

// ============================================
// Main Tag Manager Component (Enterprise-Grade)
// ============================================

interface TagManagerProps {
  tags: string[];
  onTagsChange?: (tags: string[]) => void;
  readonly?: boolean;
  maxTags?: number;
  placeholder?: string;
  className?: string;
  maxVisible?: number;
  showGrouped?: boolean;
  isLoading?: boolean;
}

export function TagManager({
  tags = [],
  onTagsChange,
  readonly = false,
  maxTags = 20,
  placeholder = "Add tag…",
  className,
  maxVisible = 3,
  showGrouped = false,
  isLoading = false,
}: TagManagerProps) {
  const [isDialogOpen, setIsDialogOpen] = React.useState(false);
  const [editingTags, setEditingTags] = React.useState<string[]>([]);
  const [newTagInput, setNewTagInput] = React.useState("");
  const [selectedPrefix, setSelectedPrefix] = React.useState<string | null>(null);

  // Initialize editing tags when dialog opens
  React.useEffect(() => {
    if (isDialogOpen) {
      setEditingTags([...tags]);
    }
  }, [isDialogOpen, tags]);

  const handleAddTag = () => {
    let tagValue = newTagInput.trim().toLowerCase();
    if (!tagValue) return;

    // Add prefix if selected
    if (selectedPrefix && !tagValue.startsWith(selectedPrefix)) {
      tagValue = `${selectedPrefix}${tagValue}`;
    }

    if (editingTags.includes(tagValue)) {
      toast({
        title: "Tag already exists",
        description: `The tag "${tagValue}" is already added.`,
        variant: "destructive",
      });
      setNewTagInput("");
      return;
    }

    if (editingTags.length >= maxTags) {
      toast({
        title: "Maximum tags reached",
        description: `You can only add up to ${maxTags} tags.`,
        variant: "destructive",
      });
      return;
    }

    setEditingTags([...editingTags, tagValue]);
    setNewTagInput("");
    setSelectedPrefix(null);
  };

  const handleRemoveTag = (tagToRemove: string) => {
    setEditingTags(editingTags.filter((tag) => tag !== tagToRemove));
  };

  const handleSave = () => {
    onTagsChange?.(editingTags);
    setIsDialogOpen(false);
  };

  const handleCancel = () => {
    setEditingTags([...tags]);
    setNewTagInput("");
    setSelectedPrefix(null);
    setIsDialogOpen(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleAddTag();
    }
  };

  if (isLoading) {
    return (
      <div className={cn("flex items-center gap-2", className)}>
        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        <span className="text-xs text-muted-foreground">Loading tags…</span>
      </div>
    );
  }

  if (readonly && tags.length === 0) {
    return null;
  }

  // important: a read-only operator gets the whole list inline. The "+N more" control that
  // reveals the rest is a DialogTrigger for the tag editor, and that editor is gated behind
  // !readonly, so slicing here removed tags 4..N from the page with nothing left to reach them.
  const visibleTags = readonly ? tags : tags.slice(0, maxVisible);
  const hiddenCount = tags.length - visibleTags.length;
  const hasMoreTags = hiddenCount > 0;

  // Dialog content
  const dialogContent = (
    <DialogContent className="sm:max-w-[600px]">
      <DialogHeader>
        <DialogTitle>Manage Tags</DialogTitle>
        <DialogDescription>
          Add tags with prefixes for better organization. Use prefixes like{" "}
          <code className="text-xs bg-muted px-1 rounded">loc_</code>,{" "}
          <code className="text-xs bg-muted px-1 rounded">vend_</code>,{" "}
          <code className="text-xs bg-muted px-1 rounded">env_</code> etc.
        </DialogDescription>
      </DialogHeader>

      <div className="space-y-4 py-4">
        {/* Current tags grouped */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium">
              Current Tags ({editingTags.length}/{maxTags})
            </p>
          </div>

          {editingTags.length === 0 ? (
            <div className="text-sm text-muted-foreground p-4 border rounded-md bg-muted/30 text-center">
              No tags yet. Add some below.
            </div>
          ) : (
            <div className="space-y-3">
              {/* Group tags by category */}
              {(() => {
                const grouped = groupTags(editingTags);
                const categoryOrder = [
                  { key: "location" as const, prefix: "loc_", name: "Location" },
                  { key: "region" as const, prefix: "reg_", name: "Region" },
                  { key: "vendor" as const, prefix: "vend_", name: "Vendor" },
                  { key: "ownership" as const, prefix: "own_", name: "Ownership" },
                  { key: "environment" as const, prefix: "env_", name: "Environment" },
                  { key: "application" as const, prefix: "app_", name: "Application" },
                  { key: "sla" as const, prefix: "sla_", name: "SLA Level" },
                  { key: "cost_center" as const, prefix: "cost_", name: "Cost Center" },
                  { key: "other" as const, prefix: "", name: "Other" },
                ];

                return categoryOrder.map(({ key, prefix, name }) => {
                  const categoryTags = grouped[key];
                  if (categoryTags.length === 0) return null;

                  const color = prefix ? TAG_CATEGORY_PREFIXES[prefix]?.color : "#6b7280";

                  return (
                    <div key={key} className="space-y-1">
                      <div className="text-xs font-medium flex items-center gap-1.5 text-muted-foreground">
                        <TagSwatch color={color} />
                        {name}
                      </div>
                      <div className="flex flex-wrap gap-1.5 pl-3.5">
                        {categoryTags.map((value) => {
                          const fullTag = prefix ? `${prefix}${value}` : value;
                          return (
                            <TagBadge
                              key={fullTag}
                              tag={fullTag}
                              onRemove={() => handleRemoveTag(fullTag)}
                              size="default"
                            />
                          );
                        })}
                      </div>
                    </div>
                  );
                });
              })()}
            </div>
          )}
        </div>

        {/* Add new tag */}
        {editingTags.length < maxTags && !readonly && (
          <div className="space-y-2">
            <p className="text-sm font-medium">Add New Tag</p>
            
            {/* Prefix selector */}
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(TAG_CATEGORY_PREFIXES).map(([prefix, info]) => (
                <TagToggle
                  key={prefix}
                  label={info.name}
                  color={info.color}
                  pressed={selectedPrefix === prefix}
                  onPressedChange={(on) => setSelectedPrefix(on ? prefix : null)}
                />
              ))}
            </div>

            {/* Input field */}
            <div className="flex gap-2">
              <div className="flex-1 relative">
                {selectedPrefix && (
                  <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm font-medium text-muted-foreground">
                    {selectedPrefix}
                  </span>
                )}
                <Input
                  placeholder={selectedPrefix ? "value" : placeholder}
                  value={newTagInput}
                  onChange={(e) => setNewTagInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  className={selectedPrefix ? "pl-14" : ""}
                />
              </div>
              <Button
                onClick={handleAddTag}
                disabled={!newTagInput.trim() || editingTags.length >= maxTags}
              >
                <Plus className="h-4 w-4 mr-1" />
                Add
              </Button>
            </div>
          </div>
        )}

        {/* Category legend */}
        <div className="text-xs text-muted-foreground space-y-2 bg-muted/50 p-3 rounded-md">
          <p className="font-medium">Tag Prefixes:</p>
          <div className="grid grid-cols-2 gap-1.5">
            {Object.entries(TAG_CATEGORY_PREFIXES).map(([prefix, info]) => (
              <div key={prefix} className="flex items-center gap-1.5">
                <TagSwatch color={info.color} />
                <code className="text-xs bg-muted px-1 rounded">{prefix}</code>
                <span>- {info.name}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Actions */}
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={handleCancel}>
          Cancel
        </Button>
        <Button onClick={handleSave}>Save Changes</Button>
      </div>
    </DialogContent>
  );

  return (
    <div className={cn("flex items-center gap-1.5 flex-wrap", className)}>
      {/* Display visible tags */}
      {visibleTags.map((tag, index) => (
        <React.Fragment key={tag}>
          <TagBadge tag={tag} size="sm" />
          {/* the gap separates the chips on screen; the comma still separates them when read aloud */}
          {index < visibleTags.length - 1 && <span className="sr-only">,</span>}
        </React.Fragment>
      ))}

      {/* Dialog trigger */}
      {!readonly && (
        <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
          {hasMoreTags ? (
            <>
              <span className="sr-only">,</span>
              <DialogTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-5 px-1 text-xs text-violet-600 hover:text-violet-700 dark:text-violet-400 dark:hover:text-violet-300 hover:bg-transparent"
                >
                  +{plural(hiddenCount, "more tag")}
                </Button>
              </DialogTrigger>
            </>
          ) : (
            tags.length < maxTags && (
              <>
                {tags.length > 0 && <span className="sr-only">,</span>}
                <DialogTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-5 px-1 gap-1 text-xs text-violet-600 hover:text-violet-700 dark:text-violet-400 dark:hover:text-violet-300"
                  >
                    <Plus className="h-3 w-3" />
                    add tag
                  </Button>
                </DialogTrigger>
              </>
            )
          )}
          {dialogContent}
        </Dialog>
      )}
    </div>
  );
}

// ============================================
// Tag Filter Component (Enhanced)
// ============================================

interface TagFilterProps {
  availableTags: string[];
  selectedTags: string[];
  onTagsChange: (tags: string[]) => void;
  showGrouped?: boolean;
  className?: string;
}

export function TagFilter({
  availableTags,
  selectedTags,
  onTagsChange,
  showGrouped = true,
  className,
}: TagFilterProps) {
  const [isOpen, setIsOpen] = React.useState(false);
  const [searchQuery, setSearchQuery] = React.useState("");

  const toggleTag = (tag: string) => {
    if (selectedTags.includes(tag)) {
      onTagsChange(selectedTags.filter((t) => t !== tag));
    } else {
      onTagsChange([...selectedTags, tag]);
    }
  };

  const clearAll = () => {
    onTagsChange([]);
  };

  if (availableTags.length === 0) {
    return null;
  }

  const filteredTags = searchQuery
    ? availableTags.filter(
        (tag) =>
          tag.toLowerCase().includes(searchQuery.toLowerCase()) ||
          getTagDisplayName(tag).toLowerCase().includes(searchQuery.toLowerCase())
      )
    : availableTags;

  const groupedAvailableTags = showGrouped ? groupTags(filteredTags) : null;

  return (
    <Popover open={isOpen} onOpenChange={setIsOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className={cn("gap-2", className)}>
          <TagIcon className="h-4 w-4" />
          Filter by Tags
          {selectedTags.length > 0 && (
            <Badge variant="secondary" className="ml-1 px-1.5 py-0 h-5">
              {selectedTags.length}
            </Badge>
          )}
          <ChevronDown className="h-3 w-3 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[350px] p-0" align="start">
        <div className="p-3 border-b">
          <div className="flex items-center gap-2">
            <Search className="h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search tags…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-8"
            />
          </div>
        </div>

        <div className="max-h-[300px] overflow-auto p-2 space-y-3">
          {showGrouped && groupedAvailableTags ? (
            // Grouped view
            <>
              {Object.entries(TAG_CATEGORY_PREFIXES).map(([prefix, info]) => {
                const categoryKey = {
                  "loc_": "location",
                  "reg_": "region",
                  "vend_": "vendor",
                  "own_": "ownership",
                  "env_": "environment",
                  "app_": "application",
                  "sla_": "sla",
                  "cost_": "cost_center",
                }[prefix] as keyof TagGrouped;

                const categoryTags = groupedAvailableTags[categoryKey] || [];
                if (categoryTags.length === 0) return null;

                return (
                  <div key={prefix} className="space-y-1.5">
                    <div className="text-xs font-medium flex items-center gap-1.5 text-muted-foreground">
                      <TagSwatch color={info.color} />
                      {info.name}
                    </div>
                    <div className="flex flex-wrap gap-1 pl-3.5">
                      {categoryTags.map((value) => {
                        const fullTag = `${prefix}${value}`;
                        return (
                          <TagToggle
                            key={fullTag}
                            label={value}
                            color={info.color}
                            pressed={selectedTags.includes(fullTag)}
                            onPressedChange={() => toggleTag(fullTag)}
                          />
                        );
                      })}
                    </div>
                  </div>
                );
              })}

              {/* Other tags */}
              {groupedAvailableTags.other.length > 0 && (
                <div className="space-y-1.5">
                  <div className="text-xs font-medium flex items-center gap-1.5 text-muted-foreground">
                    <TagSwatch color={getTagColor("")} />
                    Other
                  </div>
                  <div className="flex flex-wrap gap-1 pl-3.5">
                    {groupedAvailableTags.other.map((tag) => (
                      <TagToggle
                        key={tag}
                        label={tag}
                        color={getTagColor(tag)}
                        pressed={selectedTags.includes(tag)}
                        onPressedChange={() => toggleTag(tag)}
                      />
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : (
            // Flat view
            <div className="flex flex-wrap gap-1.5">
              {filteredTags.map((tag) => (
                <TagToggle
                  key={tag}
                  label={getTagDisplayName(tag)}
                  color={getTagColor(tag)}
                  pressed={selectedTags.includes(tag)}
                  onPressedChange={() => toggleTag(tag)}
                />
              ))}
            </div>
          )}
        </div>

        {selectedTags.length > 0 && (
          <div className="p-2 border-t flex justify-between items-center">
            <span className="text-xs text-muted-foreground">
              {selectedTags.length} selected
            </span>
            <Button variant="ghost" size="sm" onClick={clearAll} className="h-7 text-xs">
              Clear all
            </Button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}

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
import { cn } from "@/lib/utils";
import { toast } from "@/hooks/use-toast";
import {
  TagCategory,
  TagGrouped,
  TAG_CATEGORY_PREFIXES,
  classifyTag,
  getTagDisplayName,
  getTagColor,
  groupTags,
} from "@/types/api";

// ============================================
// Tag Badge Component (Enhanced with Categories)
// ============================================

interface TagBadgeProps {
  tag: string;
  onRemove?: () => void;
  showCategory?: boolean;
  size?: "sm" | "default";
  className?: string;
}

export function TagBadge({
  tag,
  onRemove,
  showCategory = false,
  size = "default",
  className,
}: TagBadgeProps) {
  const { prefix, value, category } = classifyTag(tag);
  const color = getTagColor(tag);
  const displayName = getTagDisplayName(tag);

  return (
    <Badge
      variant="secondary"
      className={cn(
        "gap-1 transition-all whitespace-nowrap",
        size === "sm" ? "text-xs py-0 h-5 px-1.5" : "text-xs py-0.5 px-2",
        className
      )}
      style={{
        backgroundColor: `${color}15`,
        borderColor: `${color}40`,
        color: color,
        border: `1px solid ${color}40`,
      }}
    >
      <span
        className="w-2 h-2 rounded-full"
        style={{ backgroundColor: color }}
      />
      {showCategory && prefix && (
        <span className="font-medium opacity-70">{category}:</span>
      )}
      <span>{displayName}</span>
      {onRemove && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          className="ml-0.5 hover:bg-black/10 rounded-full p-0.5 transition-colors"
        >
          <X className="h-3 w-3" />
        </button>
      )}
    </Badge>
  );
}

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
  placeholder = "Add tag...",
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
        <span className="text-xs text-muted-foreground">Loading tags...</span>
      </div>
    );
  }

  if (readonly && tags.length === 0) {
    return null;
  }

  const visibleTags = tags.slice(0, maxVisible);
  const hiddenCount = tags.length - maxVisible;
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
                      <div
                        className="text-xs font-medium flex items-center gap-1.5"
                        style={{ color }}
                      >
                        <span
                          className="w-2 h-2 rounded-full"
                          style={{ backgroundColor: color }}
                        />
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
                <Badge
                  key={prefix}
                  variant="outline"
                  className={cn(
                    "cursor-pointer text-xs transition-all hover:scale-105",
                    selectedPrefix === prefix && "ring-2"
                  )}
                  style={{
                    backgroundColor: selectedPrefix === prefix ? `${info.color}30` : `${info.color}10`,
                    borderColor: info.color,
                    color: info.color,
                  }}
                  onClick={() => setSelectedPrefix(selectedPrefix === prefix ? null : prefix)}
                >
                  {info.name}
                </Badge>
              ))}
            </div>

            {/* Input field */}
            <div className="flex gap-2">
              <div className="flex-1 relative">
                {selectedPrefix && (
                  <span
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-sm font-medium"
                    style={{ color: TAG_CATEGORY_PREFIXES[selectedPrefix]?.color }}
                  >
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
                <span
                  className="w-2 h-2 rounded-full"
                  style={{ backgroundColor: info.color }}
                />
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
          {index < visibleTags.length - 1 && (
            <span className="text-xs text-muted-foreground">,</span>
          )}
        </React.Fragment>
      ))}

      {/* Dialog trigger */}
      {!readonly && (
        <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
          {hasMoreTags ? (
            <>
              <span className="text-xs text-muted-foreground">,</span>
              <DialogTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-5 px-1 text-xs text-violet-600 hover:text-violet-700 dark:text-violet-400 dark:hover:text-violet-300 hover:bg-transparent"
                >
                  +{hiddenCount} more tag{hiddenCount !== 1 ? "s" : ""}...
                </Button>
              </DialogTrigger>
            </>
          ) : (
            tags.length < maxTags && (
              <>
                {tags.length > 0 && (
                  <span className="text-xs text-muted-foreground">,</span>
                )}
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
              placeholder="Search tags..."
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
                    <div
                      className="text-xs font-medium flex items-center gap-1.5"
                      style={{ color: info.color }}
                    >
                      <span
                        className="w-2 h-2 rounded-full"
                        style={{ backgroundColor: info.color }}
                      />
                      {info.name}
                    </div>
                    <div className="flex flex-wrap gap-1 pl-3.5">
                      {categoryTags.map((value) => {
                        const fullTag = `${prefix}${value}`;
                        const isSelected = selectedTags.includes(fullTag);
                        return (
                          <Badge
                            key={fullTag}
                            variant={isSelected ? "default" : "outline"}
                            className="cursor-pointer text-xs transition-all"
                            style={{
                              backgroundColor: isSelected ? info.color : `${info.color}15`,
                              borderColor: info.color,
                              color: isSelected ? "white" : info.color,
                            }}
                            onClick={() => toggleTag(fullTag)}
                          >
                            {value}
                          </Badge>
                        );
                      })}
                    </div>
                  </div>
                );
              })}

              {/* Other tags */}
              {groupedAvailableTags.other.length > 0 && (
                <div className="space-y-1.5">
                  <div className="text-xs font-medium flex items-center gap-1.5 text-gray-600">
                    <span className="w-2 h-2 rounded-full bg-gray-400" />
                    Other
                  </div>
                  <div className="flex flex-wrap gap-1 pl-3.5">
                    {groupedAvailableTags.other.map((tag) => {
                      const isSelected = selectedTags.includes(tag);
                      return (
                        <Badge
                          key={tag}
                          variant={isSelected ? "default" : "outline"}
                          className="cursor-pointer text-xs"
                          onClick={() => toggleTag(tag)}
                        >
                          {tag}
                        </Badge>
                      );
                    })}
                  </div>
                </div>
              )}
            </>
          ) : (
            // Flat view
            <div className="flex flex-wrap gap-1.5">
              {filteredTags.map((tag) => {
                const isSelected = selectedTags.includes(tag);
                const color = getTagColor(tag);
                return (
                  <Badge
                    key={tag}
                    variant={isSelected ? "default" : "outline"}
                    className="cursor-pointer text-xs transition-all"
                    style={{
                      backgroundColor: isSelected ? color : `${color}15`,
                      borderColor: color,
                      color: isSelected ? "white" : color,
                    }}
                    onClick={() => toggleTag(tag)}
                  >
                    {getTagDisplayName(tag)}
                  </Badge>
                );
              })}
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

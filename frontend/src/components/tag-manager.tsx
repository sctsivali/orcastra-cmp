"use client";

import * as React from "react";
import { X, Plus, Tag as TagIcon, Eye, EyeOff, Edit2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { toast } from "@/hooks/use-toast";

interface TagManagerProps {
  tags: string[];
  onTagsChange?: (tags: string[]) => void;
  readonly?: boolean;
  maxTags?: number;
  placeholder?: string;
  className?: string;
  maxVisible?: number; // Number of tags to show before "more tags..."
}

export function TagManager({
  tags = [],
  onTagsChange,
  readonly = false,
  maxTags = 10,
  placeholder = "Add tag...",
  className,
  maxVisible = 3,
}: TagManagerProps) {
  const [isDialogOpen, setIsDialogOpen] = React.useState(false);
  const [isPopoverOpen, setIsPopoverOpen] = React.useState(false);
  const [newTag, setNewTag] = React.useState("");
  const [editingTags, setEditingTags] = React.useState<string[]>([]);

  // Initialize editing tags when dialog opens
  React.useEffect(() => {
    if (isDialogOpen) {
      setEditingTags([...tags]);
    }
  }, [isDialogOpen, tags]);

  const handleAddTag = () => {
    const trimmedTag = newTag.trim().toLowerCase();
    if (!trimmedTag) return;
    
    if (editingTags.includes(trimmedTag)) {
      toast({
        title: "Tag already exists",
        description: `The tag "${trimmedTag}" is already added.`,
        variant: "destructive",
      });
      setNewTag("");
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

    setEditingTags([...editingTags, trimmedTag]);
    setNewTag("");
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
    setIsDialogOpen(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleAddTag();
    }
  };

  if (readonly && tags.length === 0) {
    return null;
  }

  const visibleTags = tags.slice(0, maxVisible);
  const hiddenCount = tags.length - maxVisible;
  const hasMoreTags = hiddenCount > 0;

  // Reusable Dialog Content
  const dialogContent = (
    <DialogContent className="sm:max-w-[500px]">
      <DialogHeader>
        <DialogTitle>Manage Tags</DialogTitle>
        <DialogDescription>
          Add, remove, or edit tags. Press Enter to add a new tag.
        </DialogDescription>
      </DialogHeader>
      <div className="space-y-4 py-4">
        {/* Current tags */}
        <div className="space-y-2">
          <p className="text-sm font-medium">Current Tags ({editingTags.length}/{maxTags})</p>
          <div className="flex flex-wrap gap-2 min-h-[40px] p-3 border rounded-md bg-muted/30">
            {editingTags.length === 0 ? (
              <p className="text-xs text-muted-foreground">No tags yet. Add some below.</p>
            ) : (
              editingTags.map((tag) => (
                <Badge
                  key={tag}
                  variant="secondary"
                  className="gap-1 text-xs"
                >
                  <TagIcon className="h-3 w-3" />
                  {tag}
                  <button
                    type="button"
                    onClick={() => handleRemoveTag(tag)}
                    className="ml-1 hover:bg-destructive/20 rounded-full transition-colors"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </Badge>
              ))
            )}
          </div>
        </div>

        {/* Add new tag */}
        {editingTags.length < maxTags && (
          <div className="space-y-2">
            <p className="text-sm font-medium">Add New Tag</p>
            <div className="flex gap-2">
              <Input
                placeholder={placeholder}
                value={newTag}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setNewTag(e.target.value)}
                onKeyDown={handleKeyDown}
                className="flex-1"
              />
              <Button
                onClick={handleAddTag}
                disabled={!newTag.trim() || editingTags.length >= maxTags}
              >
                <Plus className="h-4 w-4 mr-1" />
                Add
              </Button>
            </div>
          </div>
        )}

        {/* Info */}
        <div className="text-xs text-muted-foreground space-y-1 bg-muted/50 p-3 rounded-md">
          <p>• Tags are automatically converted to lowercase</p>
          <p>• Maximum {maxTags} tags allowed</p>
          <p>• Duplicate tags will be ignored</p>
        </div>
      </div>

      {/* Actions */}
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={handleCancel}>
          Cancel
        </Button>
        <Button onClick={handleSave}>
          Save Changes
        </Button>
      </div>
    </DialogContent>
  );

  return (
    <div className={cn("flex items-center gap-1.5 flex-wrap", className)}>
      {/* Display first N tags */}
      {visibleTags.map((tag, index) => (
        <React.Fragment key={tag}>
          <Badge
            variant="secondary"
            className="gap-1 text-xs py-0 h-5"
          >
            <TagIcon className="h-2.5 w-2.5" />
            {tag}
          </Badge>
          {index < visibleTags.length - 1 && (
            <span className="text-xs text-muted-foreground">,</span>
          )}
        </React.Fragment>
      ))}

      {/* Single Dialog for all cases */}
      {!readonly && (
        <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
          {/* Conditional trigger based on state */}
          {hasMoreTags ? (
            // Show "more tags..." when there are hidden tags
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
          ) : tags.length < maxTags && (
            // Show "add tag" when under limit and no hidden tags
            <>
              {tags.length > 0 && <span className="text-xs text-muted-foreground">,</span>}
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
          )}
          {dialogContent}
        </Dialog>
      )}
    </div>
  );
}

interface TagFilterProps {
  availableTags: string[];
  selectedTags: string[];
  onTagsChange: (tags: string[]) => void;
  className?: string;
}

export function TagFilter({
  availableTags,
  selectedTags,
  onTagsChange,
  className,
}: TagFilterProps) {
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

  return (
    <div className={cn("flex items-center gap-2 flex-wrap", className)}>
      <span className="text-xs font-medium text-muted-foreground">
        Filter by tags:
      </span>
      {availableTags.map((tag) => {
        const isSelected = selectedTags.includes(tag);
        return (
          <Badge
            key={tag}
            variant={isSelected ? "default" : "outline"}
            className={cn(
              "gap-1 text-xs cursor-pointer transition-all",
              isSelected && "bg-violet-500 hover:bg-violet-600"
            )}
            onClick={() => toggleTag(tag)}
          >
            <TagIcon className="h-3 w-3" />
            {tag}
          </Badge>
        );
      })}
      {selectedTags.length > 0 && (
        <Button
          variant="ghost"
          size="sm"
          onClick={clearAll}
          className="h-6 text-xs"
        >
          Clear all
        </Button>
      )}
    </div>
  );
}

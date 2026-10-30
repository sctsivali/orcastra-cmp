"use client";

import { ArrowLeft, ArrowRight, ArrowUp, Eye, EyeOff, FilePlus, FolderPlus, FolderTree, FolderUp, MoreHorizontal, Plus, RefreshCw, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SearchInput } from "@/components/ui/search-input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ExplorerWidth } from "@/hooks/use-explorer-width";
import { cn } from "@/lib/utils";
import { PathBar } from "./path-bar";

interface FileToolbarProps {
  path: string;
  mode: ExplorerWidth;
  canBack: boolean;
  canForward: boolean;
  filter: string;
  showHidden: boolean;
  treeShown: boolean;
  refreshing: boolean;
  writeReason?: string;
  editingPath: boolean;
  onEditingPath: (editing: boolean) => void;
  onBack: () => void;
  onForward: () => void;
  onUp: () => void;
  onNavigate: (path: string) => void;
  onFilter: (value: string) => void;
  onShowHidden: (value: boolean) => void;
  onToggleTree: () => void;
  onRefresh: () => void;
  onNewFolder: () => void;
  onNewFile: () => void;
  onUploadFiles: () => void;
  onUploadFolder: () => void;
}

function IconButton({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled?: boolean; children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" aria-label={label} disabled={disabled} onClick={onClick}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

export function FileToolbar(props: FileToolbarProps) {
  const { mode, writeReason } = props;
  const compact = mode === "narrow";
  return (
    <div className="border-b">
      <div className="flex min-h-[40px] items-center gap-1 px-1.5 py-1">
        <IconButton label="Show folders" onClick={props.onToggleTree}>
          <FolderTree className={cn("h-4 w-4", props.treeShown && "text-primary")} aria-hidden="true" />
        </IconButton>
        <IconButton label="Back" onClick={props.onBack} disabled={!props.canBack}>
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        </IconButton>
        {!compact && (
          <IconButton label="Forward" onClick={props.onForward} disabled={!props.canForward}>
            <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </IconButton>
        )}
        <IconButton label="Parent folder" onClick={props.onUp} disabled={props.path === "/"}>
          <ArrowUp className="h-4 w-4" aria-hidden="true" />
        </IconButton>
        <PathBar
          path={props.path}
          compact={compact}
          onNavigate={props.onNavigate}
          editing={props.editingPath}
          onEditing={props.onEditingPath}
        />
        {!compact && (
          <SearchInput
            value={props.filter}
            onChange={props.onFilter}
            placeholder="Filter this folder"
            clearLabel="Clear the folder filter"
            className="w-44 shrink-0"
          />
        )}
        <IconButton label="Refresh this folder" onClick={props.onRefresh}>
          <RefreshCw className={cn("h-4 w-4", props.refreshing && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />
        </IconButton>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="outline" className="h-8 shrink-0 gap-1 px-2" disabled={Boolean(writeReason)} title={writeReason}>
              <Plus className="h-4 w-4" aria-hidden="true" />
              {!compact && "New"}
              {compact && <span className="sr-only">New</span>}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={props.onNewFolder}>
              <FolderPlus className="mr-2 h-4 w-4" aria-hidden="true" /> New Folder
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={props.onNewFile}>
              <FilePlus className="mr-2 h-4 w-4" aria-hidden="true" /> New File
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="sm" className="h-8 shrink-0 gap-1 px-2" disabled={Boolean(writeReason)} title={writeReason}>
              <Upload className="h-4 w-4" aria-hidden="true" />
              {!compact && "Upload"}
              {compact && <span className="sr-only">Upload</span>}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={props.onUploadFiles}>
              <Upload className="mr-2 h-4 w-4" aria-hidden="true" /> Upload Files
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={props.onUploadFolder}>
              <FolderUp className="mr-2 h-4 w-4" aria-hidden="true" /> Upload Folder
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" aria-label="More options">
              <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => props.onShowHidden(!props.showHidden)}>
              {props.showHidden ? <EyeOff className="mr-2 h-4 w-4" aria-hidden="true" /> : <Eye className="mr-2 h-4 w-4" aria-hidden="true" />}
              {props.showHidden ? "Hide Hidden Files" : "Show Hidden Files"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {compact && (
              <DropdownMenuItem disabled={!props.canForward} onSelect={props.onForward}>
                <ArrowRight className="mr-2 h-4 w-4" aria-hidden="true" /> Forward
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onSelect={() => props.onEditingPath(true)}>Go To Path</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {compact && (
        <div className="px-1.5 pb-1">
          <SearchInput
            value={props.filter}
            onChange={props.onFilter}
            placeholder="Filter this folder"
            clearLabel="Clear the folder filter"
          />
        </div>
      )}
    </div>
  );
}

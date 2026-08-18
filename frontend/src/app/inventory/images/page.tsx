"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyValue } from "@/components/ui/table-text";
import { formatInstanceType, truncateFingerprint } from "@/lib/utils";
import { 
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search, UploadCloud } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { useImages } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { ImageListResponse } from "@/types/api";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { ImageUploadDialog } from "@/components/image-upload-dialog";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { Role } from "@/types/rbac";

interface ImageInfo {
  fingerprint: string;
  architecture: string;
  type: string;
  size: number;
  // The API returns aliases as plain strings (see backend ImageInfo / types/api.ts).
  aliases: string[];
  properties: Record<string, string>;
  public: boolean;
  cached: boolean;
  auto_update: boolean;
  created_at: string;
  uploaded_at: string;
  expires_at: string;
  last_used_at: string;
  node?: string;
}

export default function ImagesPage() {
  const { data: imagesByHost = [], isLoading, refetch, refreshNow, dataUpdatedAt } = useImages();
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterType, setFilterType] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [uploadOpen, setUploadOpen] = useState(false);
  const { toast } = useToast();
  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const canUpload = hasRole(Role.PARTNER);

  const sortAccessors = useMemo(
    () => ({
      fingerprint: (i: any) => i.fingerprint,
      alias: (i: any) => i.aliases?.length ?? 0,
      architecture: (i: any) => i.architecture,
      type: (i: any) => i.type,
      size: (i: any) => i.size ?? 0,
      node: (i: any) => i.node,
      status: (i: any) =>
        (i.public ? 1 : 0) + (i.cached ? 1 : 0) + (i.auto_update ? 1 : 0),
    }),
    [],
  );
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    "fingerprint",
    "asc",
    () => setCurrentPage(1),
  );

  const allImages = useMemo(() => {
    return (imagesByHost as ImageListResponse[]).flatMap((host) =>
      host.images.map((img) => ({ ...img, node: host.host }))
    );
  }, [imagesByHost]);

  const nodes = useMemo(() => {
    const nodeSet = new Set((imagesByHost as ImageListResponse[]).map((h) => h.host));
    return Array.from(nodeSet).sort();
  }, [imagesByHost]);

  const imageTypes = useMemo(() => {
    const typeSet = new Set(allImages.map((img: any) => img.type));
    return Array.from(typeSet).sort();
  }, [allImages]);

  const filteredImages = useMemo(() => {
    let filtered = allImages;

    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (img: any) =>
          img.fingerprint?.toLowerCase()?.includes(query) ||
          img.aliases?.some((alias: string) =>
            alias?.toLowerCase()?.includes(query)
          ) ||
          (img.properties?.description &&
            img.properties.description.toLowerCase().includes(query))
      );
    }

    if (filterNode !== "all") {
      filtered = filtered.filter((img) => img.node === filterNode);
    }

    if (filterType !== "all") {
      filtered = filtered.filter((img) => img.type === filterType);
    }

    return filtered;
  }, [allImages, searchQuery, filterNode, filterType]);

  const sortedImages = useMemo(() => sortItems(filteredImages), [filteredImages, sortItems]);

  const totalPages = Math.ceil(sortedImages.length / pageSize);
  const paginatedImages = sortedImages.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize
  );

  const formatSize = (bytes: number) => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + " " + sizes[i];
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Images
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor container and VM images
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton
            label="Refresh images"
            lastUpdatedAt={dataUpdatedAt}
            onRefresh={refreshNow}
          />
          {/* Partner+ only; the upload endpoint requires Partner. */}
          {canUpload && (
            <Button
              onClick={() => setUploadOpen(true)}
              disabled={isReadOnly}
              title={isReadOnly ? "Action disabled in read-only mode" : "Upload a custom image"}
            >
              <UploadCloud className="h-4 w-4 mr-2" />
              Upload Image
            </Button>
          )}
        </div>
      </div>

      <Card>
        <CardContent className="pt-6">
          <div className="flex flex-col sm:flex-row gap-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search images..."
                value={searchQuery}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  setCurrentPage(1);
                }}
                className="pl-10"
              />
            </div>
            {nodes.length > 0 && (
              <Select
                value={filterNode}
                onValueChange={(value) => {
                  setFilterNode(value);
                  setCurrentPage(1);
                }}
              >
                <SelectTrigger className="w-full sm:w-[180px]">
                  <SelectValue placeholder="All Nodes" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Nodes</SelectItem>
                  {nodes.map((node: any) => (
                    <SelectItem key={node} value={node}>
                      {node}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <Select
              value={filterType}
              onValueChange={(value) => {
                setFilterType(value);
                setCurrentPage(1);
              }}
            >
              <SelectTrigger className="w-full sm:w-[180px]">
                <SelectValue placeholder="All Types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Types</SelectItem>
                {imageTypes.map((type: any) => (
                  <SelectItem key={type} value={type}>
                    {type}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>
            All Images ({filteredImages.length})
            {(searchQuery || filterNode !== "all" || filterType !== "all")
              ? ` (filtered from ${allImages.length})`
              : ""}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Fingerprint" field="fingerprint" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Alias" field="alias" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Architecture" field="architecture" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Size" field="size" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />}
                <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedImages.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={7}
                    className="text-center text-muted-foreground"
                  >
                    No images found
                  </TableCell>
                </TableRow>
              ) : (
                paginatedImages.map((image: any, idx: number) => (
                  <TableRow key={`${image.node}-${image.fingerprint}-${idx}`}>
                    <TableCell className="font-mono">
                      {truncateFingerprint(image.fingerprint)}
                    </TableCell>
                    <TableCell>
                      {image.aliases && image.aliases.length > 0 && image.aliases[0] ? (
                        image.aliases[0]
                      ) : (
                        <TableEmptyValue label="No alias" />
                      )}
                    </TableCell>
                    <TableCell>
                      {image.architecture}
                    </TableCell>
                    <TableCell>
                      {formatInstanceType(image.type)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      {formatSize(image.size)}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell>{image.node}</TableCell>
                    )}
                    <TableCell>
                      {[
                        image.public && "Public",
                        image.cached && "Cached",
                        image.auto_update && "Auto-update",
                      ].filter(Boolean).join(", ") || <TableEmptyValue label="No flags" />}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>

          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                totalItems={filteredImages.length}
                onPageChange={setCurrentPage}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setCurrentPage(1);
                }}
              />
            </div>
          )}
        </CardContent>
      </Card>

      <ImageUploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        onUploaded={() => refetch()}
      />
    </div>
  );
}

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
import { 
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search, ArrowUp, ArrowDown } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useImages } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { ImageListResponse } from "@/types/api";

type SortField = "fingerprint" | "size" | "architecture" | "type";
type SortDirection = "asc" | "desc";

interface ImageInfo {
  fingerprint: string;
  architecture: string;
  type: string;
  size: number;
  aliases: Array<{ name: string; description: string }>;
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
  const { data: imagesByHost = [], isLoading, refetch, isRefetching } = useImages();
  
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterType, setFilterType] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [sortField, setSortField] = useState<SortField>("fingerprint");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const { toast } = useToast();

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

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
    setCurrentPage(1);
  };

  const filteredImages = useMemo(() => {
    let filtered = allImages;

    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (img: any) =>
          img.fingerprint?.toLowerCase()?.includes(query) ||
          img.aliases?.some((alias: any) =>
            alias?.name?.toLowerCase()?.includes(query)
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

    filtered.sort((a, b) => {
      let aVal, bVal;

      switch (sortField) {
        case "fingerprint":
          aVal = a.fingerprint;
          bVal = b.fingerprint;
          break;
        case "size":
          aVal = a.size || 0;
          bVal = b.size || 0;
          break;
        case "architecture":
          aVal = a.architecture || "";
          bVal = b.architecture || "";
          break;
        case "type":
          aVal = a.type || "";
          bVal = b.type || "";
          break;
        default:
          return 0;
      }

      if (aVal < bVal) return sortDirection === "asc" ? -1 : 1;
      if (aVal > bVal) return sortDirection === "asc" ? 1 : -1;
      return 0;
    });

    return filtered;
  }, [allImages, searchQuery, filterNode, filterType, sortField, sortDirection]);

  const totalPages = Math.ceil(filteredImages.length / pageSize);
  const paginatedImages = filteredImages.slice(
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

  const SortIcon = ({ field }: { field: SortField }) => {
    if (sortField !== field) return null;
    return sortDirection === "asc" ? (
      <ArrowUp className="h-3 w-3 inline ml-1" />
    ) : (
      <ArrowDown className="h-3 w-3 inline ml-1" />
    );
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
        <RefreshButton
          disabled={isRefetching}
          isRefreshing={isRefetching}
          label="Refresh images"
          onClick={() => refetch()}
        />
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
                <TableHead
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("fingerprint")}
                >
                  Fingerprint <SortIcon field="fingerprint" />
                </TableHead>
                <TableHead>Alias</TableHead>
                <TableHead
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("architecture")}
                >
                  Architecture <SortIcon field="architecture" />
                </TableHead>
                <TableHead
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("type")}
                >
                  Type <SortIcon field="type" />
                </TableHead>
                <TableHead
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => handleSort("size")}
                >
                  Size <SortIcon field="size" />
                </TableHead>
                {nodes.length > 0 && <TableHead>Node</TableHead>}
                <TableHead>Status</TableHead>
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
                    <TableCell className="font-mono text-xs">
                      {image.fingerprint.substring(0, 12)}...
                    </TableCell>
                    <TableCell>
                      {image.aliases && image.aliases.length > 0 && image.aliases[0].name ? (
                        <Badge variant="outline">{image.aliases[0].name}</Badge>
                      ) : (
                        <span className="text-muted-foreground text-xs">
                          N/A
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{image.architecture}</Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{image.type}</Badge>
                    </TableCell>
                    <TableCell className="text-xs">
                      {formatSize(image.size)}
                    </TableCell>
                    {nodes.length > 0 && (
                      <TableCell>
                        {image.node && (
                          <Badge variant="secondary">{image.node}</Badge>
                        )}
                      </TableCell>
                    )}
                    <TableCell>
                      <div className="flex gap-1">
                        {image.public && (
                          <Badge variant="default" className="text-xs">
                            Public
                          </Badge>
                        )}
                        {image.cached && (
                          <Badge variant="outline" className="text-xs">
                            Cached
                          </Badge>
                        )}
                        {image.auto_update && (
                          <Badge variant="outline" className="text-xs">
                            Auto-update
                          </Badge>
                        )}
                      </div>
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
    </div>
  );
}

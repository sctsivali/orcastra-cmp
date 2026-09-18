"use client";

import { Suspense, useState, useMemo, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow } from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { OverflowCell } from "@/components/ui/overflow-cell";
import { formatInstanceType, formatLabel } from "@/lib/utils";
import { OverflowText } from "@/components/ui/overflow-text";
import { UploadCloud, Image as ImageIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RowActions } from "@/components/ui/row-actions";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useImages, useInstances } from "@/hooks/use-lxd";
import { TableLink } from "@/components/ui/table-link";
import { ImageDetailDialog } from "./components/image-detail-dialog";
import { ImageDeleteDialog, imageLabel, type ImageRow } from "./components/image-delete-dialog";
import { imageRowActions } from "./components/image-row-actions";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { ImageInfo, ImageListResponse, VMInfo } from "@/types/api";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { ImageUploadDialog } from "@/components/image-upload-dialog";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { Role } from "@/types/rbac";
import { PageReadError } from "@/components/ui/read-error-state";
import { useRowFocus } from "@/hooks/use-row-focus";
import { requestRowFocus } from "@/lib/row-focus";
import { RowFocusNotice } from "@/components/ui/row-focus-notice";


// important: typed, not `any`. The `any` is what let this file read i.properties?.description,
// i.cached and i.auto_update for as long as it has: none of the three existed on ImageInfo or
// on the backend model, so the Flags facet could only ever offer "Public" and searchExtra
// pushed a permanent undefined. The backend now sends cached and auto_update; description has
// always been sent under its own name.

/**
 * One key for a row and for the upload dialog's report of what landed. The store is part of
 * it: a project with its own image store can hold the same fingerprint the host's default
 * store holds, and each is its own row.
 */
const imageRowKey = (i: { node?: string | null; project?: string | null; fingerprint?: string | null }) =>
  `${i.node ?? ""}/${i.project ?? "default"}/${i.fingerprint ?? ""}`;

const IMAGES = defineTable<ImageInfo>()({
  searchPlaceholder: "Search images",
  defaultSort: { field: "fingerprint", direction: "asc" },
  searchExtra: (i) => [i.description, ...(i.aliases ?? [])],
  columns: [
    {
      field: "fingerprint",
      label: "Fingerprint",
      sort: (i) => i.fingerprint,
      search: (i) => i.fingerprint, filter: fromSort("text") },
    { field: "alias", label: "Aliases", sort: (i) => i.aliases?.length ?? 0, filter: fromSort("number", { unit: "count" }) },
    {
      field: "architecture",
      label: "Architecture",
      sort: (i) => i.architecture,
      facet: (i) => i.architecture,
      // note: architecture arrives as x86_64 and must never go through formatLabel, which
      // renders it as "X86 64". It is excluded by name everywhere it is displayed.
      facetSpec: { primary: true } },
    {
      field: "type",
      label: "Type",
      sort: (i) => i.type,
      facet: (i) => i.type,
      facetSpec: { primary: true, format: formatLabel } },
    { field: "size", label: "Size", sort: (i) => i.size ?? 0, filter: fromSort("number", { unit: "bytes" }) },
    {
      field: "node",
      label: "Node",
      sort: (i) => i.node,
      facet: (i) => i.node,
      facetSpec: { role: "node", primary: true } },
    {
      field: "project",
      label: "Project",
      // The image store the row was read from: the project's own when it keeps one, otherwise
      // the host's default store. Never re-cased: a project name is registry-authored.
      sort: (i) => i.project,
      facet: (i) => i.project,
      facetSpec: { role: "project", primary: true } },
    {
      field: "flags",
      label: "Flags",
      // The Status column is three independent booleans, so a single-select Status facet
      // over it would be a lie. Multi-value, and named for what it is.
      sort: (i) => (i.public ? 1 : 0) + (i.cached ? 1 : 0) + (i.auto_update ? 1 : 0),
      facet: (i) => [i.public && "Public", i.cached && "Cached", i.auto_update && "Auto-update"].filter(Boolean) as string[],
      facetSpec: { emptyLabel: "(none)" } },
  ] });

function ImagesContent() {
  const { data: imagesByHost = [], isLoading, refetch, refreshNow, dataUpdatedAt, isError, error } = useImages();
  const [detailImage, setDetailImage] = useState<any>(null);
  // One request for the page, not one per row. The instances list is tenant-filtered server
  // side, which is why the dialog's heading says "your instances" rather than implying a total.
  const { data: instanceData } = useInstances();
  const instancesByImage = useMemo(() => {
    const byFingerprint = new Map<string, VMInfo[]>();
    for (const vm of ((instanceData as any)?.vms ?? []) as VMInfo[]) {
      // The same key the backend filters on at routers/images.py:189.
      const fingerprint = vm.config?.["volatile.base_image"];
      if (!fingerprint) continue;
      const bucket = byFingerprint.get(fingerprint);
      if (bucket) bucket.push(vm);
      else byFingerprint.set(fingerprint, [vm]);
    }
    return byFingerprint;
  }, [instanceData]);

  // A host that did not answer is returned with an empty image list, so without this it
  // is indistinguishable from a host that genuinely has no images.
  const unreachableNodes = useMemo(
    () =>
      (imagesByHost as ImageListResponse[])
        .filter((host) => host.reachable === false)
        .map((host) => host.host),
    [imagesByHost],
  );
  
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [uploadOpen, setUploadOpen] = useState(false);
  // The upload dialog stays open on "Done." so the operator can read the outcome. The focus is
  // raised when it closes, or the mark would play behind the dialog and be gone by the time
  // the list is visible.
  const uploaded = useRef<{ node: string; project: string; fingerprint: string | null; alias: string } | null>(null);
  const closeUpload = (open: boolean) => {
    setUploadOpen(open);
    if (open || !uploaded.current) return;
    const image = uploaded.current;
    uploaded.current = null;
    if (image.fingerprint) requestRowFocus("images", imageRowKey(image), image.alias || image.fingerprint);
    void refetch();
  };
  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const canUpload = hasRole(Role.PARTNER);
  // The same gate as upload, and the same gate the DELETE endpoint applies. A tenant gets no
  // actions column at all rather than a column of reasons it cannot act.
  const canManage = canUpload;

  const { navigate } = useNavigationProgress();
  const [deleteTarget, setDeleteTarget] = useState<ImageRow | null>(null);

  const resetPage = () => setCurrentPage(1);

  const allImages = useMemo(() => {
    return (imagesByHost as ImageListResponse[]).flatMap((host) =>
      host.images.map((img) => ({ ...img, node: host.host }))
    );
  }, [imagesByHost]);

  const nodes = useMemo(() => {
    // note: an unreachable host is excluded. Offering it as a filter that can only ever
    // yield an empty table reads as "this host has no images".
    const nodeSet = new Set(
      (imagesByHost as ImageListResponse[])
        .filter((h) => h.reachable !== false)
        .map((h) => h.host),
    );
    return Array.from(nodeSet).sort();
  }, [imagesByHost]);

  // The Node column is gated on reachable hosts, so on this page the facet can vanish while
  // rows are still on screen. That is unlike its siblings, where `nodes` empties only when
  // the whole scope does.
  const availableFields = useMemo(
    () => (nodes.length > 0 ? IMAGES.facetFields : IMAGES.facetFields.filter((f) => f !== "node")),
    [nodes.length],
  );

  const filters = useTableFilters(IMAGES, allImages as any[], {
    onFilterChange: resetPage,
    availableFields });

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    IMAGES.sortAccessors,
    IMAGES.defaultSort.field,
    IMAGES.defaultSort.direction,
    resetPage,
  );

  const sortedImages = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );
  const focus = useRowFocus({
    list: "images",
    rows: allImages,
    orderedRows: sortedImages,
    keyOf: imageRowKey,
    pageSize,
    setPage: setCurrentPage,
    dataUpdatedAt,
    clearFilters: filters.clearAll,
  });

  const totalPages = Math.ceil(sortedImages.length / pageSize);
  // note: the cast restores what allImages already guarantees. Every row is built there with
  // `node: host.host`, but the filter and sort pipeline is generic over ImageInfo, whose own
  // `node` is optional, so the narrowing is lost in transit rather than being unproven.
  const paginatedImages = sortedImages.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize
  ) as ImageRow[];

  // Header, cells and the empty row's span move together. Deriving all three from one number
  // is what keeps zz-colspan-parity green when a column appears or disappears.
  const columnCount = 7 + (nodes.length > 0 ? 1 : 0) + (canManage ? 1 : 0);

  const actionsFor = (image: ImageRow) =>
    imageRowActions({
      image,
      unreachable: unreachableNodes.includes(image.node),
      readOnly: isReadOnly,
      onCreate: navigate,
      onDelete: setDeleteTarget,
    });

  const formatSize = (bytes: number) => {
    if (bytes === 0) return "0 B";
    const k = 1000; // decimal: the label says KB/MB/GB, not KiB
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + " " + sizes[i];
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  // A failed read is not an empty list. Rendered before the table so the filtered-empty row
  // cannot blame a filter for a request that did not return.
  if (isError && imagesByHost.length === 0) {
    return (
      <PageReadError
        title="Images"
        description="Manage hypervisor container and VM images"
        noun="images"
        error={error}
        onRetry={() => void refetch()}
      />
    );
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

      <UnreachableNodesBanner noun="images" nodes={unreachableNodes} />

      <Card>
        <CardHeader className="space-y-4">
          <CardTitle>All Images</CardTitle>
          <TableToolbar filters={filters} noun="images" />
        </CardHeader>
        <CardContent>
          {focus.hidden && (
            <RowFocusNotice label={focus.hidden.label} onShow={focus.showHidden} className="mb-4" />
          )}
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Fingerprint" field="fingerprint" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Aliases" field="alias" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Architecture" field="architecture" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Size" field="size" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {nodes.length > 0 && <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />}
                <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Flags" field="flags" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                {/* Not sortable and not a value, so it carries its name for a screen reader
                    rather than a visible label that would read as a sixth column of data. */}
                {canManage && (
                  <TableHead className="w-12 text-right">
                    <span className="sr-only">Actions</span>
                  </TableHead>
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedImages.length === 0 ? (
                // note: the outage case is not repeated here. UnreachableNodesBanner above
                // already names which nodes are missing and why; saying it twice, in two
                // treatments, reads as two separate problems.
                <TableEmptyRow
                  // Derived from the header's own conditions rather than hardcoded. note:
                  // unlike the sibling pages, `nodes` here excludes a host flagged
                  // unreachable, so that column can vanish from a populated table, not only
                  // from an empty scope. The actions column moves with the role.
                  colSpan={columnCount}
                  icon={ImageIcon}
                  title="No images found"
                  description={
                    filters.activeFilterCount > 0
                      ? `No image matches the current filters. ${filters.activeFilterCount} filter${filters.activeFilterCount === 1 ? "" : "s"} active.`
                      : undefined
                  }
                  action={
                    filters.activeFilterCount > 0 ? (
                      <Button variant="outline" size="sm" onClick={() => filters.clearAll()}>
                        Clear filters
                      </Button>
                    ) : undefined
                  }
                />
              ) : (
                paginatedImages.map((image: ImageRow) => (
                  <TableRow key={imageRowKey(image)} {...focus.rowProps(imageRowKey(image))}>
                    {/* important: the row names itself by whatever a human or a registry wrote,
                        and only falls back to the fingerprint when an image carries neither. It
                        used to name itself with a 12 character slice of the fingerprint, so the
                        aliases control announced "Show all 3 aliases on 1a4eed830630...". */}
                    <TableCell className="max-w-[18rem] font-mono">
                      <OverflowText
                        value={image.fingerprint}
                        label={`fingerprint for ${imageLabel(image)}`}
                        reveal="always"
                        mono
                      >
                        {(expanded) => (
                          <TableLink
                            wrap={expanded}
                            onClick={() => setDetailImage(image)}
                            aria-label={`Show details for image ${image.fingerprint}`}
                          >
                            {image.fingerprint}
                          </TableLink>
                        )}
                      </OverflowText>
                    </TableCell>
                    <TableCell>
                      <OverflowCell
                        items={image.aliases?.filter(Boolean)}
                        preview={1}
                        noun="aliases"
                        ownerLabel={imageLabel(image)}
                        emptyLabel="No alias"
                        itemLabel="alias"
                      />
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
                    <TableCell>{image.project}</TableCell>
                    <TableCell>
                      {[
                        image.public && "Public",
                        image.cached && "Cached",
                        image.auto_update && "Auto-update",
                      ].filter(Boolean).join(", ") || <TableEmptyValue label="No flags" />}
                    </TableCell>
                    {canManage && (
                      <TableCell className="text-right">
                        <RowActions
                          triggerLabel={`Actions for image ${imageLabel(image)}`}
                          actions={actionsFor(image)}
                        />
                      </TableCell>
                    )}
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
                totalItems={sortedImages.length}
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

      <ImageDetailDialog
        image={detailImage}
        instances={detailImage ? instancesByImage.get(detailImage.fingerprint) ?? [] : []}
        open={!!detailImage}
        onOpenChange={(next) => {
          if (!next) setDetailImage(null);
        }}
      />

      <ImageUploadDialog
        open={uploadOpen}
        onOpenChange={closeUpload}
        onUploaded={(image) => {
          uploaded.current = image;
          void refetch();
        }}
      />

      <ImageDeleteDialog
        target={deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onDeleted={() => void refetch()}
        localDependents={(fingerprint) =>
          (instancesByImage.get(fingerprint) ?? []).map((vm) => vm.name)
        }
      />
    </div>
  );
}

// Suspense is required because useTableFilters reads useSearchParams, which opts the
// component out of prerendering.
export default function ImagesPage() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <ImagesContent />
    </Suspense>
  );
}

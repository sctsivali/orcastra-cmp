"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Box, FolderKanban, Search, Server, UserCog } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandSeparator } from "@/components/ui/command";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useAllProjects, useNodesStatus, useVMs } from "@/hooks/use-lxd";
import { useClusterLabel } from "@/hooks/use-node-picker-options";
import { inventoryLinks } from "@/lib/inventory-links";
import { formatCount } from "@/lib/utils";
import { NAV_GROUPS, filterGroups, withEditionGroups, itemVisible } from "@/components/sidebar/nav-config";
import { EDITION_NAV_GROUPS } from "@/components/sidebar/edition-nav";
import { useQuery } from "@tanstack/react-query";
import { miniApi } from "../api";
import { MIN_RESOURCE_QUERY, rank, type Field } from "./rank";

const PER_GROUP = 6;

/** Ctrl+K and Cmd+K, except where a terminal or a graphical console owns the keyboard. */
function isPaletteShortcut(event: KeyboardEvent): boolean {
  if (event.key.toLowerCase() !== "k" || !(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return false;
  const target = event.target instanceof Element ? event.target : null;
  // Ctrl+K deletes to the end of the line in a shell, and a console forwards every key to the guest.
  return !target?.closest(".xterm, canvas, [role='application']");
}

/** The header's search box and the palette it opens. */
export function MiniHeaderSearch(): JSX.Element {
  const [open, setOpen] = useState(false);
  const [shortcut, setShortcut] = useState("Ctrl K");

  useEffect(() => {
    // Read after mount, so the server render and the first client render agree.
    if (/mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)) setShortcut("⌘K");
    const onKey = (event: KeyboardEvent) => {
      if (!isPaletteShortcut(event)) return;
      event.preventDefault();
      setOpen((value) => !value);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="h-9 w-9 justify-center p-0 text-muted-foreground md:w-64 md:justify-start md:px-3"
        aria-label="Search pages and resources"
        aria-keyshortcuts="Control+K Meta+K"
        onClick={() => setOpen(true)}
      >
        <Search className="h-4 w-4 md:mr-2" aria-hidden="true" />
        <span className="hidden md:inline">Search</span>
        <kbd className="pointer-events-none ml-auto hidden select-none rounded border bg-muted px-1.5 font-mono text-xs font-medium md:inline">
          {shortcut}
        </kbd>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-xl gap-0 overflow-hidden p-0">
          <DialogTitle className="sr-only">Search</DialogTitle>
          <DialogDescription className="sr-only">
            Go to a page, or find an instance, project, cluster or operator you can see.
          </DialogDescription>
          {open && <PaletteBody onDone={() => setOpen(false)} />}
        </DialogContent>
      </Dialog>
    </>
  );
}

interface Hit {
  key: string;
  title: string;
  detail?: string;
  href: string;
  icon: LucideIcon;
}

// Mounted only while the palette is open: its reads share the list pages' cache keys, so an
// opened palette shows what those pages already loaded and nothing polls while it is closed.
function PaletteBody({ onDone }: { onDone: () => void }): JSX.Element {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const { isAdmin, isPartner, isTenant } = usePermissions();
  const flags = useMemo(() => ({ isAdmin, isPartner, isTenant }), [isAdmin, isPartner, isTenant]);
  const resourceQuery = query.trim().length >= MIN_RESOURCE_QUERY ? query.trim() : "";
  const operatorsItem = EDITION_NAV_GROUPS.flatMap((g) => g.items).find((i) => i.id === "operators");
  const seesOperators = !!operatorsItem && itemVisible(operatorsItem, flags);
  // Clusters is a Partner page: a Tenant's read of /nodes is refused and audited.
  const seesClusters = isAdmin || isPartner;

  const go = (href: string) => {
    onDone();
    router.push(href);
  };

  return (
    <Command shouldFilter={false} loop className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground">
      <CommandInput value={query} onValueChange={setQuery} placeholder="Search pages, instances, projects" aria-label="Search" />
      <CommandList className="max-h-[min(60vh,420px)]">
        <CommandEmpty>Nothing you can see matches “{query.trim()}”.</CommandEmpty>
        <PageHits query={query.trim()} flags={flags} onPick={go} />
        {resourceQuery ? (
          <>
            <InstanceHits query={resourceQuery} onPick={go} />
            <ProjectHits query={resourceQuery} onPick={go} />
            {seesClusters && <ClusterHits query={resourceQuery} onPick={go} />}
            {seesOperators && <OperatorHits query={resourceQuery} onPick={go} />}
          </>
        ) : (
          query.trim() && (
            <CommandItem disabled value="hint:min-length" className="text-muted-foreground">
              Type one more character to search resources.
            </CommandItem>
          )
        )}
      </CommandList>
      <div className="flex items-center gap-3 border-t px-3 py-2 text-xs text-muted-foreground">
        <span><kbd className="font-mono">{"↑↓"}</kbd> move</span>
        <span><kbd className="font-mono">Enter</kbd> open</span>
        <span><kbd className="font-mono">Esc</kbd> close</span>
      </div>
    </Command>
  );
}

// Long names wrap rather than clip: an option in a listbox cannot hold a reveal button of its own.
function HitRow({ hit, onPick }: { hit: Hit; onPick: (href: string) => void }): JSX.Element {
  return (
    <CommandItem value={hit.key} onSelect={() => onPick(hit.href)} className="items-start gap-2">
      <hit.icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block break-words">{hit.title}</span>
        {hit.detail && <span className="block break-words text-xs text-muted-foreground">{hit.detail}</span>}
      </span>
    </CommandItem>
  );
}

function HitGroup({ heading, hits, total, more, state, onPick }: {
  heading: string;
  hits: Hit[];
  total: number;
  more?: Hit;
  state?: ReactNode;
  onPick: (href: string) => void;
}): JSX.Element | null {
  if (!state && hits.length === 0) return null;
  return (
    <>
      <CommandSeparator />
      <CommandGroup heading={total > hits.length ? `${heading} (${formatCount(total)})` : heading}>
        {/* An item, not a paragraph: the empty state counts items, and a group still loading is not empty. */}
        {state ? <CommandItem disabled value={`state:${heading}`} className="text-muted-foreground">{state}</CommandItem> : null}
        {hits.map((hit) => <HitRow key={hit.key} hit={hit} onPick={onPick} />)}
        {more && total > hits.length && (
          <CommandItem value={more.key} onSelect={() => onPick(more.href)} className="gap-2 text-muted-foreground">
            <more.icon className="h-4 w-4 shrink-0" aria-hidden="true" />
            <span>{more.title}</span>
          </CommandItem>
        )}
      </CommandGroup>
    </>
  );
}

function readState(query: { isPending: boolean; isError: boolean; data?: unknown }, noun: string): ReactNode {
  if (query.isPending) return `Loading ${noun}…`;
  if (query.isError && !query.data) return `${noun[0].toUpperCase()}${noun.slice(1)} could not be loaded.`;
  return null;
}

function PageHits({ query, flags, onPick }: {
  query: string;
  flags: { isAdmin: boolean; isPartner: boolean; isTenant: boolean };
  onPick: (href: string) => void;
}): JSX.Element | null {
  const pages = useMemo(() => {
    const groups = filterGroups(withEditionGroups(NAV_GROUPS), flags);
    return groups.flatMap((group) =>
      group.items
        .filter((item) => item.href)
        .map((item) => ({ item, group: group.label ?? "" }))
    );
  }, [flags]);
  // With nothing typed, every page the role can open, in sidebar order.
  const ranked = query ? rank(pages, query, (p) => [[p.item.label, 1], [p.group, 0.6]] as Field[], 8) : { items: pages };
  const hits: Hit[] = ranked.items.map(({ item, group }) => ({
    key: `page:${item.id}`,
    title: item.label,
    detail: group || undefined,
    href: item.href as string,
    icon: item.icon,
  }));
  if (hits.length === 0) return null;
  return (
    <CommandGroup heading={query ? "Pages" : "Go to"}>
      {hits.map((hit) => <HitRow key={hit.key} hit={hit} onPick={onPick} />)}
    </CommandGroup>
  );
}

function InstanceHits({ query, onPick }: { query: string; onPick: (href: string) => void }): JSX.Element | null {
  const vms = useVMs();
  const clusterLabel = useClusterLabel();
  const ranked = rank(vms.data?.vms ?? [], query, (vm) => [
    [vm.name, 1],
    [(vm.ip_addresses ?? []).join(" "), 0.85],
    [(vm.tags ?? []).join(" "), 0.9],
    [vm.project, 0.7],
    [vm.description, 0.5],
  ], PER_GROUP);
  const hits = ranked.items.map((vm) => ({
    key: `instance:${vm.host}/${vm.project ?? "default"}/${vm.name}`,
    title: vm.name,
    detail: [String(vm.status), vm.project, clusterLabel(vm.host)].filter(Boolean).join(" · "),
    href: inventoryLinks.instances({ q: vm.name, host: vm.host, project: vm.project }),
    icon: Box,
  }));
  return (
    <HitGroup
      heading="Instances"
      hits={hits}
      total={ranked.total}
      state={readState(vms, "instances")}
      more={{ key: "more:instances", title: `Show all ${formatCount(ranked.total)} matching instances`, href: inventoryLinks.instances({ q: query }), icon: Search }}
      onPick={onPick}
    />
  );
}

function ProjectHits({ query, onPick }: { query: string; onPick: (href: string) => void }): JSX.Element | null {
  const projects = useAllProjects();
  const clusterLabel = useClusterLabel();
  const rows = useMemo(
    () => (projects.data ?? []).flatMap((host) => host.projects.map((project) => ({ ...project, host: host.host }))),
    [projects.data]
  );
  const ranked = rank(rows, query, (p) => [[p.name, 1], [(p.tags ?? []).join(" "), 0.9], [p.description, 0.5]], PER_GROUP);
  // There is no project page: a project opens as the instance list filtered to it.
  const hits = ranked.items.map((p) => ({
    key: `project:${p.host}/${p.name}`,
    title: p.name,
    detail: clusterLabel(p.host),
    href: inventoryLinks.instances({ host: p.host, project: p.name }),
    icon: FolderKanban,
  }));
  return <HitGroup heading="Projects" hits={hits} total={ranked.total} state={readState(projects, "projects")} onPick={onPick} />;
}

function ClusterHits({ query, onPick }: { query: string; onPick: (href: string) => void }): JSX.Element | null {
  const nodes = useNodesStatus();
  // The display name lives in the availability read, as on every other cluster label.
  const clusterLabel = useClusterLabel();
  const ranked = rank(nodes.data?.nodes ?? [], query, (n) => [
    [clusterLabel(n.name), 1],
    [n.name, 1],
    [n.endpoint, 0.7],
    [(n.tags ?? []).join(" "), 0.9],
  ], PER_GROUP);
  const hits = ranked.items.map((n) => ({
    key: `cluster:${n.name}`,
    title: clusterLabel(n.name),
    detail: n.connected ? "Connected" : "Not connected",
    href: inventoryLinks.clusters({ q: n.name }),
    icon: Server,
  }));
  return <HitGroup heading="Clusters" hits={hits} total={ranked.total} state={readState(nodes, "clusters")} onPick={onPick} />;
}

function OperatorHits({ query, onPick }: { query: string; onPick: (href: string) => void }): JSX.Element | null {
  const { scopeKey } = useActorQueryScope();
  // The Operators page's own key and read, so either one warms the other.
  const operators = useQuery({ queryKey: ["mini-operators", scopeKey], queryFn: () => miniApi.listOperators(), staleTime: 30_000 });
  const active = useMemo(() => (operators.data ?? []).filter((o) => o.status === "active"), [operators.data]);
  const ranked = rank(active, query, (o) => [[o.email, 1], [o.display_name, 0.9]], PER_GROUP);
  const hits = ranked.items.map((o) => ({
    key: `operator:${o.uuid}`,
    title: o.display_name || o.email,
    detail: o.display_name ? `${o.email} · ${o.role}` : o.role,
    href: inventoryLinks.operators({ q: o.email }),
    icon: UserCog,
  }));
  return <HitGroup heading="Operators" hits={hits} total={ranked.total} state={readState(operators, "operators")} onPick={onPick} />;
}


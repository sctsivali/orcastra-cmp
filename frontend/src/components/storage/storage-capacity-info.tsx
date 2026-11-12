import { InfoToggletip } from "@/components/ui/info-toggletip";

/** The (i) beside a cluster's Storage heading: what the figure adds up. */
export function StorageCapacityInfo({ className }: { className?: string }) {
  return (
    <InfoToggletip label="About cluster storage" className={className}>
      <p>Used and usable space across every storage pool on this cluster.</p>
      <p>
        A pool on shared storage, such as Ceph, is counted once rather than once per cluster
        member. Local pools are added up per member.
      </p>
      <p>The bar turns amber from 75% and red from 90%.</p>
    </InfoToggletip>
  );
}

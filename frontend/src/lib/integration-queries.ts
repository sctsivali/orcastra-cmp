import type { QueryClient } from "@tanstack/react-query";

/**
 * Refetch what a write to an integration key or to an organization's owner can change.
 *
 * ["integrations"] holds the API key list and the Synced Organizations panel. The organization
 * pickers in User Groups, the policy editor and tenant access read the same list under
 * ["integration-organizations"].
 *
 * note: the dev line routes this through reconcileMutationImpact(..., "integration.write") in
 * lib/mutation-coherence.ts, which this line does not have.
 */
export async function invalidateIntegrationQueries(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["integrations"] }),
    queryClient.invalidateQueries({ queryKey: ["integration-organizations"] }),
  ]);
}

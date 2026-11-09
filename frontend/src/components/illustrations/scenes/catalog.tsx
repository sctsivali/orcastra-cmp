import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Building, Key } from "../iso/parts";
import { IsoBox, IsoLink, IsoShadow } from "../iso/primitives";

// Catalog and pointer cards: solutions, providers, add-ons and keys.

/** A platform of app blocks with the next one arriving: a portfolio that grows. */
export function PortfolioIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[23, 22, 121, 91]}>
      <IsoShadow box={[-26, -20, 52, 40]} />
      <IsoBox at={[-26, -20, 0]} size={[52, 40, 5]} material="paper" />
      <IsoBox at={[-20, -14, 5]} size={[13, 13, 13]} material="tint" />
      <IsoBox at={[-3, -14, 5]} size={[13, 13, 13]} material="paper" />
      <IsoBox at={[-20, 3, 5]} size={[13, 13, 13]} material="paper" />
      <IsoBox at={[-3, 3, 5]} size={[13, 13, 13]} material="ghost" />
      <IsoLink path={[[3.5, 9.5, 27], [3.5, 9.5, 19]]} fine />
      <IsoBox at={[-3, 3, 27]} size={[13, 13, 13]} material="ink" />
    </IllustrationFrame>
  );
}

/** Providers joined into one network, with room for the next. */
export function EcosystemIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[19.5, 8.5, 132.5, 99.5]}>
      <IsoShadow box={[-12, -22, 16, 16]} />
      <IsoShadow box={[-28, 6, 14, 14]} />
      <IsoShadow box={[12, -26, 14, 14]} />
      <IsoShadow box={[8, 12, 12, 12]} dashed />
      <IsoLink path={[[-21, 13, 0], [-4, -14, 0], [19, -19, 0], [14, 18, 0]]} />
      <Building at={[-12, -22, 0]} size={[16, 16, 34]} material="ink" />
      <Building at={[-28, 6, 0]} size={[14, 14, 22]} />
      <Building at={[12, -26, 0]} size={[14, 14, 26]} />
      <Building at={[8, 12, 0]} size={[12, 12, 16]} material="ghost" />
    </IllustrationFrame>
  );
}

/** A module lowered into its socket beside the ones already installed. */
export function ExtensionsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[22, 19.5, 122.5, 92]}>
      <IsoShadow box={[-24, -20, 48, 40]} />
      <IsoBox at={[-24, -20, 0]} size={[48, 40, 5]} material="paper" />
      <IsoBox at={[-19, -15, 5]} size={[12, 12, 11]} material="paper" />
      <IsoBox at={[-19, 3, 5]} size={[12, 12, 11]} material="tint" />
      <IsoBox at={[0, -10, 5]} size={[14, 14, 4]} material="paper" open />
      <IsoLink path={[[7, -3, 26], [7, -3, 11]]} fine />
      <IsoBox at={[0, -10, 26]} size={[14, 14, 11]} material="ink" />
    </IllustrationFrame>
  );
}

/** A key standing in its holder. */
export function KeyVaultIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[17.5, 0.5, 131.5, 98.5]}>
      <IsoShadow box={[-14, -10, 28, 20]} />
      <IsoBox at={[-14, -10, 0]} size={[28, 20, 7]} material="tint" />
      <Key origin={[-12, 0, 3]} />
    </IllustrationFrame>
  );
}

import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Building, Key } from "../iso/parts";
import { IsoBox, IsoLink, IsoShadow } from "../iso/primitives";

// Catalog and pointer cards: solutions, providers, add-ons and keys.

/** A platform of app blocks with the next one arriving: a portfolio that grows. */
export function PortfolioIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[11.5, 14.5, 137, 102.5]}>
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
    <IllustrationFrame {...props} view={[6, 3, 152.5, 114.5]}>
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
    <IllustrationFrame {...props} view={[12.5, 14.5, 135, 101]}>
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
    <IllustrationFrame {...props} view={[8.5, 5.5, 143, 107]}>
      <IsoShadow box={[-24, -14, 48, 28]} />
      <IsoBox at={[-24, -14, 0]} size={[48, 28, 7]} material="tint" />
      <IsoBox at={[-5, -1, 7]} size={[10, 8, 2]} material="paper" open />
      <Key origin={[-9.6, 1, 5.8]} scale={0.8} />
    </IllustrationFrame>
  );
}

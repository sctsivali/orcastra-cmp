import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Building, DOCUMENT_OUTLINE, Pawn, ServerStack, SHIELD_OUTLINE } from "../iso/parts";
import { IsoBall, IsoBox, IsoCylinder, IsoDisc, IsoExtrude, IsoGlyph, IsoInset, IsoLink, IsoMark, IsoShadow } from "../iso/primitives";
import { scaleOutline } from "../iso/projection";

// First-run empty states: what will be here once the first one exists. A ghost outline marks the
// thing still to come, so the scene reads as an invitation rather than an error.

export function EmptyClustersIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[13, 10, 134, 100.5]}>
      <IsoShadow box={[-21, -15, 42, 30]} />
      <IsoBox at={[-21, -15, 0]} size={[42, 30, 3]} material="paper" />
      <ServerStack at={[-18, -12, 3]} units={3} w={36} d={24} h={8} material="ghost" />
      <IsoMark at={[0, 0, 41]} />
    </IllustrationFrame>
  );
}

export function EmptyInstancesIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[13, 10, 134, 100.5]}>
      <IsoShadow box={[-20, -16, 40, 32]} />
      <IsoBox at={[-20, -16, 0]} size={[40, 32, 16]} material="paper" open />
      <IsoBox at={[-7, -5, 26]} size={[14, 14, 14]} material="ghost" />
    </IllustrationFrame>
  );
}

/** Copies kept over time: plates stacked like layers, the newest on top. */
export function SnapshotsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[24, 20.5, 112, 84]}>
      <IsoShadow box={[-17, -12, 34, 24]} />
      <IsoLink path={[[17, 12, 3], [17, 12, 20]]} fine />
      <IsoLink path={[[-17, 12, 3], [-17, 12, 20]]} fine />
      <IsoBox at={[-17, -12, 0]} size={[34, 24, 3]} material="paper" />
      <IsoBox at={[-17, -12, 10]} size={[34, 24, 3]} material="tint" />
      <IsoBox at={[-17, -12, 20]} size={[34, 24, 3]} material="ink" />
    </IllustrationFrame>
  );
}

/** A policy page with the shield that enforces it. */
export function PoliciesIllustration(props: IllustrationProps) {
  const page = [-24, 0, 0] as const;
  const shield = [4, 12, 0] as const;
  return (
    <IllustrationFrame {...props} view={[12.5, 9, 126.5, 95]}>
      <IsoShadow box={[-24, -3, 26, 3]} />
      <IsoShadow box={[4, 8, 20, 4]} />
      <IsoExtrude plane="left" origin={page} outline={DOCUMENT_OUTLINE} depth={3} material="paper" />
      <IsoInset plane="left" origin={page} rect={[4, 24, 12, 2]} kind="detail" />
      <IsoInset plane="left" origin={page} rect={[4, 18.5, 18, 2]} kind="detail" fine />
      <IsoInset plane="left" origin={page} rect={[4, 13, 15, 2]} kind="detail" fine />
      <IsoInset plane="left" origin={page} rect={[4, 7.5, 18, 2]} kind="detail" fine />
      <IsoExtrude plane="left" origin={shield} outline={scaleOutline(SHIELD_OUTLINE, 0.62)} depth={4} material="ink" />
      <IsoGlyph plane="left" origin={shield} path={[[5.5, 10.8], [8.5, 7.6], [14, 14.4]]} onInk />
    </IllustrationFrame>
  );
}

export function UsersIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[38, 25.5, 97.5, 73]}>
      <IsoShadow circle={[-20, -2, 7]} />
      <IsoShadow circle={[8, -22, 7]} />
      <IsoShadow circle={[4, 8, 7]} />
      <Pawn at={[-20, -2, 0]} />
      <Pawn at={[8, -22, 0]} material="tint" />
      <Pawn at={[4, 8, 0]} material="ink" />
    </IllustrationFrame>
  );
}

/** Two organizations kept in step. */
export function OrganizationsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[20, 2.5, 127, 95]}>
      <IsoShadow box={[10, -26, 16, 16]} />
      <IsoShadow box={[-28, 2, 16, 16]} />
      <Building at={[10, -26, 0]} size={[16, 16, 32]} material="tint" />
      <IsoLink path={[[-20, 10, 24], [18, -18, 32]]} />
      <IsoMark at={[-1, -4, 28]} size={3} />
      <Building at={[-28, 2, 0]} size={[16, 16, 24]} material="ink" />
    </IllustrationFrame>
  );
}

/** A map tile with routes between sites and a pin on one of them. */
export function RegionsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[19, 24.5, 122.5, 91.5]}>
      <IsoShadow box={[-26, -20, 52, 40]} />
      <IsoBox at={[-26, -20, 0]} size={[52, 40, 4]} material="tint" />
      <IsoLink path={[[-16, -10, 4], [2, 8, 4], [16, -12, 4]]} />
      <IsoMark at={[-16, -10, 4]} size={3.5} />
      <IsoMark at={[16, -12, 4]} size={3.5} />
      <IsoBox at={[1.25, 7.25, 4]} size={[1.5, 1.5, 14]} material="ink" />
      <IsoBall at={[2, 8, 22]} radius={5.5} material="ink" />
    </IllustrationFrame>
  );
}

/** A disc in its open case, with the mark for the first one still to add. */
export function IsoImagesIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[21, 25.5, 118.5, 88.5]}>
      <IsoShadow box={[-22, -22, 44, 44]} />
      <IsoBox at={[-22, -22, 0]} size={[44, 44, 4]} material="paper" />
      <IsoCylinder at={[0, 0, 4]} radius={19} height={2} material="tint" />
      <IsoDisc plane="top" origin={[0, 0, 6]} centre={[0, 0]} radius={6} kind="panel" />
      <IsoDisc plane="top" origin={[0, 0, 6]} centre={[0, 0]} radius={2.2} kind="screen" />
      <IsoMark at={[0, 0, 24]} />
    </IllustrationFrame>
  );
}

// Inventory lists before their first entry: the object, and a ghost or a mark for what comes next.

/** A switch with its ports, linked to the two hosts still to come. */
export function NetworksIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[31.5, 30, 96.5, 72.5]}>
      <IsoShadow box={[-20, -8, 40, 16]} />
      <IsoLink path={[[-20, -2, 3], [-28, -4, 3]]} />
      <IsoLink path={[[-2, -8, 3], [-4, -28, 3]]} />
      <IsoBox at={[-34, -10, 0]} size={[12, 12, 10]} material="ghost" />
      <IsoBox at={[-10, -34, 0]} size={[12, 12, 10]} material="ghost" />
      <IsoBox at={[-20, -8, 0]} size={[40, 16, 7]} material="paper" />
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <IsoInset key={i} plane="left" origin={[-20, 8, 0]} rect={[4 + i * 5.6, 2, 3.4, 3]} kind="detail" />
      ))}
    </IllustrationFrame>
  );
}

/** One volume in place and the slot for the next. */
export function EmptyVolumesIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[38.5, 30, 85, 63.5]}>
      <IsoShadow circle={[14, -16, 12]} dashed />
      <IsoShadow circle={[-8, 4, 14]} />
      <IsoMark at={[14, -16, 16]} />
      <IsoCylinder at={[-8, 4, 0]} radius={14} height={7} material="paper" />
      <IsoCylinder at={[-8, 4, 7]} radius={14} height={3} material="tint" />
    </IllustrationFrame>
  );
}

/** A pool: a slab holding a volume, with room left on it. */
export function EmptyPoolsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[23, 24.5, 118.5, 89]}>
      <IsoShadow box={[-24, -22, 48, 40]} />
      <IsoBox at={[-24, -22, 0]} size={[48, 40, 4]} material="tint" />
      <IsoShadow circle={[11, -11, 9]} dashed />
      <IsoMark at={[11, -11, 14]} />
      <IsoCylinder at={[-9, 3, 4]} radius={10} height={7} material="paper" />
    </IllustrationFrame>
  );
}

/** A sealed image package, and the next one still to arrive. */
export function ImagesIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[42.5, 19, 111.5, 83.5]}>
      <IsoShadow box={[-14, -12, 28, 24]} />
      <IsoBox at={[8, -32, 0]} size={[20, 18, 14]} material="ghost" />
      <IsoBox at={[-14, -12, 0]} size={[28, 24, 20]} material="paper" />
      <IsoInset plane="top" origin={[-14, -12, 20]} rect={[12, 0, 4, 24]} kind="detail" />
      <IsoInset plane="left" origin={[-14, 12, 0]} rect={[4, 9, 10, 6]} kind="panel" />
    </IllustrationFrame>
  );
}

/** An address plan: a grid of cells with the first one taken. */
export function IpamIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[24, 28.5, 112.5, 84.5]}>
      <IsoShadow box={[-24, -18, 48, 36]} />
      <IsoBox at={[-24, -18, 0]} size={[48, 36, 4]} material="tint" />
      {[0, 1, 2, 3].flatMap((i) => [0, 1, 2].map((j) => (
        <IsoInset key={`${i}-${j}`} plane="top" origin={[-24, -18, 4]} rect={[3 + i * 11, 3 + j * 11, 8, 8]} kind="screen" />
      )))}
      <IsoBox at={[-10, -4, 4]} size={[8, 8, 6]} material="ink" />
    </IllustrationFrame>
  );
}

// A settings card with three sliders, for a profile that sets an instance's defaults.
const CARD_OUTLINE = [[0, 0], [32, 0], [32, 24], [0, 24]] as const;

/** A profile card standing in front of the one still to come. */
export function ProfilesIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[44.5, 30.5, 88.5, 66.5]}>
      <IsoShadow box={[-16, 1, 32, 3]} />
      <IsoExtrude plane="left" origin={[-6, -10, 0]} outline={CARD_OUTLINE} depth={3} material="ghost" />
      <IsoExtrude plane="left" origin={[-16, 4, 0]} outline={CARD_OUTLINE} depth={3} material="paper" />
      {[0, 1, 2].map((i) => (
        <g key={i}>
          <IsoInset plane="left" origin={[-16, 4, 0]} rect={[4, 5.3 + i * 6.5, 24, 1.4]} kind="detail" />
          <IsoDisc plane="left" origin={[-16, 4, 0]} centre={[9 + i * 7, 6 + i * 6.5]} radius={2.4} kind="panel" />
        </g>
      ))}
    </IllustrationFrame>
  );
}

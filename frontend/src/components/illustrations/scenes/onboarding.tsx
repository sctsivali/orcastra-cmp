import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Building, Calendar, DOCUMENT_OUTLINE, Pawn, ServerStack, SHIELD_OUTLINE } from "../iso/parts";
import { IsoBall, IsoBox, IsoExtrude, IsoGlyph, IsoGround, IsoInset, IsoLink, IsoMark } from "../iso/primitives";
import { scaleOutline } from "../iso/projection";

// First-run empty states: what will be here once the first one exists. A ghost outline marks the
// thing still to come, so the scene reads as an invitation rather than an error.

export function EmptyClustersIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround dashed />
      <IsoBox at={[-21, -15, 0]} size={[42, 30, 3]} material="paper" />
      <ServerStack at={[-18, -12, 3]} units={3} w={36} d={24} h={8} material="ghost" />
      <IsoMark at={[0, 0, 41]} />
    </IllustrationFrame>
  );
}

export function EmptyInstancesIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={[-20, -16, 0]} size={[40, 32, 16]} material="paper" open />
      <IsoBox at={[-7, -5, 26]} size={[14, 14, 14]} material="ghost" />
    </IllustrationFrame>
  );
}

/** Copies kept over time: plates stacked like layers, the newest on top. */
export function SnapshotsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
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
    <IllustrationFrame {...props}>
      <IsoGround />
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
    <IllustrationFrame {...props}>
      <IsoGround />
      <Pawn at={[-20, -2, 0]} />
      <Pawn at={[8, -22, 0]} material="tint" />
      <Pawn at={[4, 8, 0]} material="ink" />
    </IllustrationFrame>
  );
}

/** Two organizations kept in step. */
export function OrganizationsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <Building at={[10, -26, 0]} size={[16, 16, 32]} material="ink" />
      <IsoLink path={[[-20, 10, 24], [18, -18, 32]]} />
      <IsoMark at={[-1, -4, 28]} size={3} />
      <Building at={[-28, 2, 0]} size={[16, 16, 24]} />
    </IllustrationFrame>
  );
}

export function SchedulesIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <Calendar at={[-19, -3, 0]} />
    </IllustrationFrame>
  );
}

/** A map tile with routes between sites and a pin on one of them. */
export function RegionsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={[-26, -20, 0]} size={[52, 40, 4]} material="tint" />
      <IsoLink path={[[-16, -10, 4], [2, 8, 4], [16, -12, 4]]} />
      <IsoMark at={[-16, -10, 4]} size={3.5} />
      <IsoMark at={[16, -12, 4]} size={3.5} />
      <IsoBox at={[1.25, 7.25, 4]} size={[1.5, 1.5, 14]} material="ink" />
      <IsoBall at={[2, 8, 22]} radius={5.5} material="ink" />
    </IllustrationFrame>
  );
}

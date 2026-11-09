import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { ServerStack } from "../iso/parts";
import { IsoBox, IsoExtrude, IsoFlat, IsoGround, IsoInset } from "../iso/primitives";

// KPI tiles show these at 64px, so each is one or two large forms and its fine detail drops out.
// A scene names the tile's subject; the number beside it is the only thing that changes.

export function ClustersIllustration(props: IllustrationProps) {
  const unit = { units: 2, w: 26, d: 18, h: 9, gap: 2 } as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <ServerStack at={[4, -28, 0]} hero={1} {...unit} />
      <ServerStack at={[-30, -2, 0]} {...unit} />
    </IllustrationFrame>
  );
}

export function InstancesIllustration(props: IllustrationProps) {
  const face = [-28, 14, 0] as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={[6, -26, 0]} size={[14, 14, 14]} material="tint" />
      <IsoBox at={[6, -26, 14]} size={[14, 14, 14]} material="ink" />
      <IsoBox at={[-28, -6, 0]} size={[26, 20, 26]} material="paper" />
      <IsoInset plane="left" origin={face} rect={[3.5, 8, 19, 13]} kind="panel" />
    </IllustrationFrame>
  );
}

const BLOCK = { at: [-20, -15, 0] as const, size: [40, 30, 24] as const };
const FRONT = [BLOCK.at[0], BLOCK.at[1] + BLOCK.size[1], 0] as const;

export function RunningIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={BLOCK.at} size={BLOCK.size} material="paper" />
      {/* note: the apex is lifted by half its run to undo the face's skew, so the mark reads as a
          play sign at 64px instead of a corner wedge. */}
      <IsoFlat plane="left" origin={FRONT} outline={[[15, 4], [15, 20], [29, 19]]} kind="detail" />
    </IllustrationFrame>
  );
}

export function StoppedIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={BLOCK.at} size={BLOCK.size} material="paper" />
      <IsoInset plane="left" origin={FRONT} rect={[13, 5, 5, 14]} kind="detail" />
      <IsoInset plane="left" origin={FRONT} rect={[22, 5, 5, 14]} kind="detail" />
    </IllustrationFrame>
  );
}

/** A warning sign on its post. */
export function WarningsIllustration(props: IllustrationProps) {
  const sign = [-17, 2, 12] as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={[-10, -6, 0]} size={[20, 12, 3]} material="paper" />
      <IsoBox at={[-1.5, -1.5, 3]} size={[3, 3, 9]} material="paper" />
      <IsoExtrude plane="left" origin={sign} outline={[[0, 0], [34, 0], [17, 29]]} depth={4} material="ink" />
      <IsoInset plane="left" origin={sign} rect={[15.6, 10, 2.8, 11]} kind="cut" />
      <IsoInset plane="left" origin={sign} rect={[15.6, 4, 2.8, 3.2]} kind="cut" />
    </IllustrationFrame>
  );
}

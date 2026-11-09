import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { ServerStack } from "../iso/parts";
import { IsoBox, IsoExtrude, IsoFlat, IsoInset, IsoShadow } from "../iso/primitives";

// KPI tiles show these at 64px, so each is one or two large forms and its fine detail drops out.
// A scene names the tile's subject; the number beside it is the only thing that changes.

export function ClustersIllustration(props: IllustrationProps) {
  // The second cluster stands half a step behind the first so the pair reads as one form.
  const unit = { units: 2, w: 24, d: 16, h: 8, gap: 2 } as const;
  return (
    <IllustrationFrame {...props} view={[22.5, 19, 114.5, 86]}>
      <IsoShadow box={[-8.5, -21, 24, 16]} />
      <IsoShadow box={[-15.5, 5, 24, 16]} />
      <ServerStack at={[-8.5, -21, 0]} hero={1} {...unit} />
      <ServerStack at={[-15.5, 5, 0]} {...unit} />
    </IllustrationFrame>
  );
}

export function InstancesIllustration(props: IllustrationProps) {
  const face = [-28, 14, 0] as const;
  return (
    <IllustrationFrame {...props} view={[18, -1, 128.5, 96.5]}>
      <IsoShadow box={[6, -26, 14, 14]} />
      <IsoShadow box={[-28, -6, 26, 20]} />
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
    <IllustrationFrame {...props} view={[14, 10.5, 132.5, 99.5]}>
      <IsoShadow box={[-20, -15, 40, 30]} />
      <IsoBox at={BLOCK.at} size={BLOCK.size} material="tint" />
      {/* note: the apex is lifted by half its run to undo the face's skew, so the mark reads as a
          play sign at 64px instead of a corner wedge. */}
      <IsoFlat plane="left" origin={FRONT} outline={[[15, 4], [15, 20], [29, 19]]} kind="detail" />
    </IllustrationFrame>
  );
}

export function StoppedIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[14, 10.5, 132.5, 99.5]}>
      <IsoShadow box={[-20, -15, 40, 30]} />
      <IsoBox at={BLOCK.at} size={BLOCK.size} material="tint" />
      <IsoInset plane="left" origin={FRONT} rect={[13, 5, 5, 14]} kind="detail" />
      <IsoInset plane="left" origin={FRONT} rect={[22, 5, 5, 14]} kind="detail" />
    </IllustrationFrame>
  );
}

/** A warning sign on its post, in front of the server it is about. */
export function WarningsIllustration(props: IllustrationProps) {
  const sign = [-11, 6, 12] as const;
  return (
    <IllustrationFrame {...props} view={[18.5, 15, 117.5, 88]}>
      <IsoShadow box={[-30, -20, 24, 16]} />
      <IsoShadow box={[-4, -2, 20, 12]} />
      <ServerStack at={[-30, -20, 0]} units={2} w={24} d={16} h={8} />
      <IsoBox at={[-4, -2, 0]} size={[20, 12, 3]} material="paper" />
      <IsoBox at={[4.5, 2.5, 3]} size={[3, 3, 9]} material="paper" />
      <IsoExtrude plane="left" origin={sign} outline={[[0, 0], [34, 0], [17, 29]]} depth={4} material="tint" />
      <IsoInset plane="left" origin={sign} rect={[15.2, 10, 3.6, 11]} kind="detail" />
      <IsoInset plane="left" origin={sign} rect={[15.2, 3.5, 3.6, 3.6]} kind="detail" />
    </IllustrationFrame>
  );
}

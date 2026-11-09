import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Folder, ServerStack, SHIELD_OUTLINE } from "../iso/parts";
import { IsoBox, IsoExtrude, IsoGlyph, IsoGround, IsoInset, IsoLink, IsoMark, IsoDisc } from "../iso/primitives";
import { circleOutline, scaleOutline, type Pt } from "../iso/projection";

// The platform's own capabilities, for the Home feature cards and the get-started call to action.

/** Several clusters joined to one control plane. */
export function MultiClusterIllustration(props: IllustrationProps) {
  const small = { units: 2, w: 16, d: 12, h: 5, gap: 1.5 } as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoLink path={[[0, 0, 0], [22, -22, 0]]} />
      <IsoLink path={[[0, 0, 0], [-20, 20, 0]]} />
      <IsoLink path={[[0, 0, 0], [22, 22, 0]]} />
      <ServerStack at={[14, -28, 0]} {...small} />
      <ServerStack at={[-12, -8, 0]} units={3} w={24} d={16} h={7} hero={2} />
      <ServerStack at={[-28, 14, 0]} {...small} />
      <ServerStack at={[14, 16, 0]} {...small} />
    </IllustrationFrame>
  );
}

/** A virtual machine beside a stack of containers. */
export function WorkloadsIllustration(props: IllustrationProps) {
  const vm = { at: [-28, -2, 0] as const, size: [26, 20, 24] as const };
  const face = [vm.at[0], vm.at[1] + vm.size[1], 0] as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={[6, -24, 0]} size={[11, 11, 11]} material="paper" />
      <IsoBox at={[17, -24, 0]} size={[11, 11, 11]} material="tint" />
      <IsoBox at={[6, -13, 0]} size={[11, 11, 11]} material="paper" />
      <IsoBox at={[6, -24, 11]} size={[11, 11, 11]} material="ink" />
      <IsoBox at={vm.at} size={vm.size} material="paper" />
      <IsoInset plane="left" origin={face} rect={[3.5, 8, 19, 13]} kind="panel" />
      <IsoInset plane="left" origin={face} rect={[6, 16.5, 9, 1.8]} kind="screen" fine />
      <IsoInset plane="left" origin={face} rect={[6, 12.5, 13, 1.8]} kind="screen" fine />
      <IsoInset plane="left" origin={face} rect={[9.5, 2.5, 7, 2]} kind="detail" />
    </IllustrationFrame>
  );
}

/** Bars rising from a plinth: watching the fleet over time. */
export function MonitoringIllustration(props: IllustrationProps) {
  const bars = [11, 20, 15, 28];
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={[-26, -17, 0]} size={[50, 32, 4]} material="paper" />
      {bars.map((h, i) => (
        <IsoBox key={i} at={[-21 + i * 11.5, -7, 4]} size={[8, 8, h]} material={i === bars.length - 1 ? "ink" : i === 1 ? "tint" : "paper"} />
      ))}
    </IllustrationFrame>
  );
}

// Half a dial, standing upright.
const DIAL: Pt[] = [...circleOutline(17, 0, 17, 36).filter(([, v]) => v >= -1e-6).sort((a, b) => b[0] - a[0])];

/** A dial on its base: speed under load. */
export function PerformanceIllustration(props: IllustrationProps) {
  const face = [-17, 3, 6] as const;
  const ticks = [30, 60, 90, 120, 150].map((deg) => (deg * Math.PI) / 180);
  const needle = (52 * Math.PI) / 180;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={[-21, -11, 0]} size={[42, 20, 6]} material="ink" />
      <IsoExtrude plane="left" origin={face} outline={DIAL} depth={6} material="paper" />
      {ticks.map((t, i) => (
        <IsoGlyph key={i} plane="left" origin={face} path={[[17 + Math.cos(t) * 11.5, Math.sin(t) * 11.5], [17 + Math.cos(t) * 14.5, Math.sin(t) * 14.5]]} />
      ))}
      <IsoGlyph plane="left" origin={face} path={[[17, 2], [17 + Math.cos(needle) * 10.5, 2 + Math.sin(needle) * 10.5]]} />
      <IsoDisc plane="left" origin={face} centre={[17, 2]} radius={2.4} kind="panel" />
    </IllustrationFrame>
  );
}

/** The orcastra hexagon as a shield, standing on a plinth. */
export function SecurityIllustration(props: IllustrationProps) {
  const face = [-15, 3, 6] as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoBox at={[-18, -12, 0]} size={[36, 22, 6]} material="paper" />
      <IsoExtrude plane="left" origin={face} outline={scaleOutline(SHIELD_OUTLINE, 0.94)} depth={5} material="ink" />
      <IsoGlyph plane="left" origin={face} path={[[8.5, 17], [13.2, 11.8], [22, 22.5]]} onInk />
    </IllustrationFrame>
  );
}

/** Capacity that grows: one, two, then three units. */
export function ScaleIllustration(props: IllustrationProps) {
  const unit = { w: 20, d: 14, h: 6, gap: 2 } as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoLink path={[[-18, 17, 0], [2, 1, 0], [20, -19, 0]]} fine />
      <ServerStack at={[-28, 10, 0]} units={1} {...unit} />
      <ServerStack at={[-8, -6, 0]} units={2} {...unit} />
      <ServerStack at={[10, -26, 0]} units={3} hero={2} {...unit} />
    </IllustrationFrame>
  );
}

/** A server in place and the slot for the next one. */
export function GetStartedIllustration(props: IllustrationProps) {
  const unit = { units: 2, w: 24, d: 16, h: 7, gap: 2 } as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <IsoLink path={[[-16, 10, 0], [18, -14, 0]]} />
      <ServerStack at={[6, -22, 0]} material="ghost" {...unit} />
      <IsoMark at={[18, -14, 26]} />
      <ServerStack at={[-28, 2, 0]} hero={1} {...unit} />
    </IllustrationFrame>
  );
}

/** Folders stacked, the working one on top. */
export function ProjectsIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <Folder at={[-20, -15, 0]} />
      <Folder at={[-20, -15, 8]} material="tint" />
      <Folder at={[-20, -15, 16]} material="ink" />
    </IllustrationFrame>
  );
}

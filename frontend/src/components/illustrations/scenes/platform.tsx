import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Folder, ServerStack, SHIELD_OUTLINE } from "../iso/parts";
import { IsoBox, IsoExtrude, IsoGlyph, IsoInset, IsoLink, IsoMark, IsoDisc, IsoShadow } from "../iso/primitives";
import { circleOutline, scaleOutline, type Pt } from "../iso/projection";

// The platform's own capabilities, for the Home feature cards and the get-started call to action.

/** Several clusters joined to one control plane. */
export function MultiClusterIllustration(props: IllustrationProps) {
  // The two member clusters stand behind the control plane, not beside it, so the group stays
  // compact enough to read at the size of one object.
  const small = { units: 2, w: 18, d: 13, h: 6, gap: 1.5 } as const;
  return (
    <IllustrationFrame {...props} view={[17.5, 8, 124.5, 93.5]}>
      <IsoShadow box={[-37, -10.5, 18, 13]} />
      <IsoShadow box={[-13, -34.5, 18, 13]} />
      <IsoShadow box={[-13, -9, 26, 18]} />
      <IsoLink path={[[0, 0, 0], [-28, -4, 0]]} />
      <IsoLink path={[[0, 0, 0], [-4, -28, 0]]} />
      <ServerStack at={[-37, -10.5, 0]} {...small} />
      <ServerStack at={[-13, -34.5, 0]} {...small} />
      <ServerStack at={[-13, -9, 0]} units={3} w={26} d={18} h={8} hero={2} />
    </IllustrationFrame>
  );
}

/** A virtual machine beside a stack of containers. */
export function WorkloadsIllustration(props: IllustrationProps) {
  const vm = { at: [-28, -2, 0] as const, size: [26, 20, 24] as const };
  const face = [vm.at[0], vm.at[1] + vm.size[1], 0] as const;
  return (
    <IllustrationFrame {...props} view={[18.5, 1, 129, 97]}>
      <IsoShadow box={[6, -24, 22, 11]} />
      <IsoShadow box={[6, -13, 11, 11]} />
      <IsoShadow box={[-28, -2, 26, 20]} />
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
    <IllustrationFrame {...props} view={[19.5, 20.5, 121.5, 91]}>
      <IsoShadow box={[-26, -17, 50, 32]} />
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
    <IllustrationFrame {...props} view={[34.5, 33.5, 93.5, 70]}>
      <IsoShadow box={[-21, -11, 42, 20]} />
      <IsoBox at={[-21, -11, 0]} size={[42, 20, 6]} material="paper" />
      <IsoExtrude plane="left" origin={face} outline={DIAL} depth={6} material="ink" />
      {ticks.map((t, i) => (
        <IsoGlyph key={i} plane="left" origin={face} path={[[17 + Math.cos(t) * 11.5, Math.sin(t) * 11.5], [17 + Math.cos(t) * 14.5, Math.sin(t) * 14.5]]} onInk />
      ))}
      <IsoGlyph plane="left" origin={face} path={[[17, 2], [17 + Math.cos(needle) * 10.5, 2 + Math.sin(needle) * 10.5]]} onInk />
      <IsoDisc plane="left" origin={face} centre={[17, 2]} radius={2.4} kind="cut" />
    </IllustrationFrame>
  );
}

/** The orcastra hexagon as a shield, standing on a plinth. */
export function SecurityIllustration(props: IllustrationProps) {
  const face = [-15, 3, 6] as const;
  return (
    <IllustrationFrame {...props} view={[18.5, 15, 125, 94]}>
      <IsoShadow box={[-22, -14, 44, 26]} />
      <IsoBox at={[-22, -14, 0]} size={[44, 26, 6]} material="paper" />
      <IsoExtrude plane="left" origin={face} outline={scaleOutline(SHIELD_OUTLINE, 0.94)} depth={5} material="ink" />
      <IsoGlyph plane="left" origin={face} path={[[8.5, 17], [13.2, 11.8], [22, 22.5]]} onInk />
    </IllustrationFrame>
  );
}

/** Capacity that grows: one, two, then three units. */
export function ScaleIllustration(props: IllustrationProps) {
  // Each taller stack stands one step behind the last, so they overlap like bars instead of
  // spreading across the frame.
  const unit = { w: 22, d: 16, h: 7, gap: 2 } as const;
  return (
    <IllustrationFrame {...props} view={[14.5, 11.5, 131, 98.5]}>
      <IsoShadow box={[-4, -31, 22, 16]} />
      <IsoShadow box={[-11, -8, 22, 16]} />
      <IsoShadow box={[-18, 15, 22, 16]} />
      <ServerStack at={[-4, -31, 0]} units={3} hero={2} {...unit} />
      <ServerStack at={[-11, -8, 0]} units={2} {...unit} />
      <ServerStack at={[-18, 15, 0]} units={1} {...unit} />
    </IllustrationFrame>
  );
}

/** A server in place and the slot for the next one. */
export function GetStartedIllustration(props: IllustrationProps) {
  const unit = { units: 2, w: 24, d: 16, h: 7, gap: 2 } as const;
  return (
    <IllustrationFrame {...props} view={[20, 7.5, 127, 95]}>
      <IsoShadow box={[6, -22, 24, 16]} dashed />
      <IsoShadow box={[-28, 2, 24, 16]} />
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
    <IllustrationFrame {...props} view={[17, 15, 126, 94.5]}>
      <IsoShadow box={[-20, -15, 40, 30]} />
      <Folder at={[-20, -15, 0]} />
      <Folder at={[-20, -15, 8]} material="ink" />
      <Folder at={[-20, -15, 16]} material="tint" />
    </IllustrationFrame>
  );
}

import * as React from "react";
import { IsoBall, IsoBox, IsoCylinder, IsoDisc, IsoExtrude, IsoFlat, IsoGlyph, IsoInset, type Material } from "./primitives";
import { circleOutline, scaleOutline, type Pt, type Vec3 } from "./projection";

// Compound objects shared by several scenes. Keeping them here is what makes a server in the
// Multi-Cluster scene the same server as the one in the empty state: one object, one drawing.

const markOn = (material: Material) => (material === "ink" ? "cut" : "detail");

/** One rack unit: a block with two status lights and a vent on its front panel. */
export function ServerUnit({ at, w = 40, d = 26, h = 9, material = "paper" }: { at: Vec3; w?: number; d?: number; h?: number; material?: Material }) {
  const [x, y, z] = at;
  const face: Vec3 = [x, y + d, z];
  const mark = markOn(material);
  return (
    <g>
      <IsoBox at={at} size={[w, d, h]} material={material} />
      {material !== "ghost" && (
        <>
          <IsoInset plane="left" origin={face} rect={[3.5, h / 2 - 1, 2.6, 2]} kind={mark} />
          <IsoInset plane="left" origin={face} rect={[7.6, h / 2 - 1, 2.6, 2]} kind={mark} fine />
          {[0, 1, 2, 3].map((i) => (
            <IsoInset key={i} plane="left" origin={face} rect={[w - 19 + i * 4.2, 2.6, 1.3, h - 5.2]} kind={mark} fine />
          ))}
        </>
      )}
    </g>
  );
}

/** Units stacked upward; `hero` paints one unit (counted from the bottom) solid. */
export function ServerStack({
  at,
  units = 3,
  hero,
  w = 40,
  d = 26,
  h = 9,
  gap = 2,
  material = "paper",
}: {
  at: Vec3;
  units?: number;
  hero?: number;
  w?: number;
  d?: number;
  h?: number;
  gap?: number;
  material?: Material;
}) {
  const [x, y, z] = at;
  return (
    <g>
      {Array.from({ length: units }, (_, i) => (
        <ServerUnit key={i} at={[x, y, z + i * (h + gap)]} w={w} d={d} h={h} material={hero === i ? "ink" : material} />
      ))}
    </g>
  );
}

/** A processor package: the substrate, pins on the two visible edges, and the die. */
export function Chip({ at, size = 36, die = "ink" as Material }: { at: Vec3; size?: number; die?: Material }) {
  const [x, y, z] = at;
  const pins = [0, 1, 2, 3, 4];
  const step = size / (pins.length + 1);
  // Pins stand in front of the two visible edges, so they are drawn after the substrate.
  return (
    <g>
      <IsoBox at={at} size={[size, size, 5]} material="paper" />
      {pins.map((i) => (
        <IsoBox key={`r${i}`} at={[x + size, y + step * (i + 1) - 1.2, z + 0.5]} size={[3.5, 2.4, 1.6]} material="paper" />
      ))}
      {pins.map((i) => (
        <IsoBox key={`l${i}`} at={[x + step * (i + 1) - 1.2, y + size, z + 0.5]} size={[2.4, 3.5, 1.6]} material="paper" />
      ))}
      <IsoBox at={[x + size * 0.25, y + size * 0.25, z + 5]} size={[size * 0.5, size * 0.5, 3]} material={die} />
    </g>
  );
}

/** Disks stacked like platters; the top one can be the hero. */
export function DiskStack({ at, radius = 19, height = 8, count = 3, gap = 3, heroTop = true }: { at: Vec3; radius?: number; height?: number; count?: number; gap?: number; heroTop?: boolean }) {
  const [x, y, z] = at;
  return (
    <g>
      {Array.from({ length: count }, (_, i) => (
        <IsoCylinder key={i} at={[x, y, z + i * (height + gap)]} radius={radius} height={height} material={heroTop && i === count - 1 ? "ink" : "paper"} />
      ))}
    </g>
  );
}

/** A memory module standing on its edge, chips on the visible face and contacts along the foot. */
export function RamStick({ at, material = "paper" }: { at: Vec3; material?: Material }) {
  const [x, y, z] = at;
  const [w, d, h] = [54, 3, 17];
  const face: Vec3 = [x, y + d, z];
  const mark = markOn(material);
  return (
    <g>
      <IsoBox at={at} size={[w, d, h]} material={material} />
      {[0, 1, 2, 3].map((i) => (
        <IsoInset key={i} plane="left" origin={face} rect={[4 + i * 12.5, 5.5, 9, 8]} kind={material === "ink" ? "cut" : "panel"} />
      ))}
      {Array.from({ length: 12 }, (_, i) => (
        <IsoInset key={`c${i}`} plane="left" origin={face} rect={[3 + i * 4.2, 1, 2, 2.6]} kind={mark} fine />
      ))}
    </g>
  );
}

/** A graphics card: the shroud, two fans with hubs, and the bracket at its end. */
export function GpuCard({ at }: { at: Vec3 }) {
  const [x, y, z] = at;
  const [w, d, h] = [52, 12, 22];
  const face: Vec3 = [x, y + d, z];
  return (
    <g>
      <IsoBox at={[x - 3, y - 1, z]} size={[3, d + 2, h + 6]} material="tint" />
      <IsoBox at={at} size={[w, d, h]} material="paper" />
      {[14, 38].map((u) => (
        <g key={u}>
          <IsoDisc plane="left" origin={face} centre={[u, h / 2]} radius={8.5} kind="panel" />
          <IsoDisc plane="left" origin={face} centre={[u, h / 2]} radius={3} kind="screen" />
        </g>
      ))}
    </g>
  );
}

/** A building block with a window grid, for an organization or a provider. */
export function Building({ at, size = [18, 18, 34] as Vec3, material = "paper" }: { at: Vec3; size?: Vec3; material?: Material }) {
  const [x, y, z] = at;
  const [w, d, h] = size;
  const mark = markOn(material);
  const rows = Math.max(1, Math.floor((h - 6) / 7));
  return (
    <g>
      <IsoBox at={at} size={size} material={material} />
      {material !== "ghost" &&
        Array.from({ length: rows }, (_, r) => (
          <g key={r}>
            {[0, 1].map((c) => (
              <IsoInset key={`l${c}`} plane="left" origin={[x, y + d, z]} rect={[3 + c * (w / 2), h - 7 - r * 7, w / 2 - 6, 3.2]} kind={mark} fine={r > 0} />
            ))}
            {[0, 1].map((c) => (
              <IsoInset key={`r${c}`} plane="right" origin={[x + w, y + d, z]} rect={[3 + c * (d / 2), h - 7 - r * 7, d / 2 - 6, 3.2]} kind={mark} fine={r > 0} />
            ))}
          </g>
        ))}
    </g>
  );
}

/** A flat folder: the body and its tab along the back edge. */
export function Folder({ at, material = "paper" }: { at: Vec3; material?: Material }) {
  const [x, y, z] = at;
  return (
    <g>
      <IsoBox at={at} size={[40, 30, 4]} material={material} />
      <IsoBox at={[x + 3, y, z + 4]} size={[14, 6, 2]} material={material} />
    </g>
  );
}

/** A person token: a short drum for the body and a ball for the head. */
export function Pawn({ at, material = "paper" }: { at: Vec3; material?: Material }) {
  const [x, y, z] = at;
  return (
    <g>
      <IsoCylinder at={at} radius={7} height={11} material={material} />
      <IsoBall at={[x, y, z + 18.5]} radius={6} material={material} />
    </g>
  );
}

// A key standing upright: the bow on top, the blade down into its stand, teeth on the right.
const KEY_OUTLINE: Pt[] = (() => {
  const [cu, cv, r] = [12, 40, 11];
  const start = (285.8 * Math.PI) / 180;
  const end = ((254.2 + 360) * Math.PI) / 180;
  const arc: Pt[] = [];
  for (let i = 0; i <= 24; i += 1) {
    const t = start + ((end - start) * i) / 24;
    arc.push([cu + Math.cos(t) * r, cv + Math.sin(t) * r]);
  }
  return [...arc, [9, 4], [15, 4], [15, 8], [20.5, 8], [20.5, 11.5], [15, 11.5], [15, 15], [19, 15], [19, 18.5], [15, 18.5]];
})();

export function Key({ origin, material = "ink", scale = 1 }: { origin: Vec3; material?: Material; scale?: number }) {
  return (
    <g>
      <IsoExtrude plane="left" origin={origin} outline={scaleOutline(KEY_OUTLINE, scale)} depth={4} material={material} />
      <IsoDisc plane="left" origin={origin} centre={[12 * scale, 40 * scale]} radius={4.6 * scale} kind={material === "ink" ? "cut" : "panel"} />
    </g>
  );
}

// The shackle of a padlock: an inverted U band.
const SHACKLE: Pt[] = (() => {
  const [cu, cv] = [14, 6];
  const outer = circleOutline(cu, cv, 11, 40).filter(([, v]) => v >= cv - 1e-6).sort((a, b) => b[0] - a[0]);
  const inner = circleOutline(cu, cv, 6.5, 40).filter(([, v]) => v >= cv - 1e-6).sort((a, b) => a[0] - b[0]);
  return [[25, 0], ...outer, [3, 0], [7.5, 0], ...inner, [20.5, 0]];
})();

/** A padlock: the body is the hero, the shackle rises from its top. */
export function Padlock({ at }: { at: Vec3 }) {
  const [x, y, z] = at;
  const [w, d, h] = [28, 12, 22];
  const face: Vec3 = [x, y + d, z];
  return (
    <g>
      <IsoBox at={at} size={[w, d, h]} material="ink" />
      <IsoDisc plane="left" origin={face} centre={[14, 13]} radius={3} kind="cut" />
      <IsoInset plane="left" origin={face} rect={[13, 5.5, 2, 6]} kind="cut" />
      <IsoExtrude plane="left" origin={[x, y + d / 2 + 2, z + h]} outline={SHACKLE} depth={4} material="paper" />
    </g>
  );
}

/** The orcastra hexagon as a shield outline, so security reads in the brand's own shape. */
export const SHIELD_OUTLINE: Pt[] = [
  [0, 30],
  [16, 37],
  [32, 30],
  [32, 13],
  [16, 0],
  [0, 13],
];

/** A page with its corner folded, standing upright. */
export const DOCUMENT_OUTLINE: Pt[] = [
  [0, 0],
  [26, 0],
  [26, 27],
  [19, 34],
  [0, 34],
];

// The brand hexagon standing on a flat side, with a status drawn on it.
const TOKEN: Pt[] = [[4.6, 0], [13.8, 0], [18.4, 8], [13.8, 16], [4.6, 16], [0, 8]];
const CLOCK_RING: Pt[] = [...circleOutline(9.2, 8, 4.6, 20), [13.8, 8]];

export type TokenGlyph = "check" | "clock" | "cross";

/** A status token standing upright in front of what it is about: valid, about to lapse, or ended. */
export function StatusToken({ origin, glyph }: { origin: Vec3; glyph: TokenGlyph }) {
  return (
    <g>
      <IsoExtrude plane="left" origin={origin} outline={TOKEN} depth={3} material="ink" />
      {glyph === "check" && <IsoGlyph plane="left" origin={origin} path={[[5.4, 8.2], [8.2, 5.2], [13.2, 11]]} onInk />}
      {glyph === "cross" && (
        <>
          <IsoGlyph plane="left" origin={origin} path={[[6.2, 4.6], [12.2, 11.4]]} onInk />
          <IsoGlyph plane="left" origin={origin} path={[[12.2, 4.6], [6.2, 11.4]]} onInk />
        </>
      )}
      {glyph === "clock" && (
        <>
          <IsoGlyph plane="left" origin={origin} path={CLOCK_RING} onInk />
          <IsoGlyph plane="left" origin={origin} path={[[9.2, 11], [9.2, 8], [11.6, 8]]} onInk />
        </>
      )}
    </g>
  );
}

/** A certificate standing upright: a page with its lines of text and a seal. */
export function Certificate({ origin, material = "paper" }: { origin: Vec3; material?: Material }) {
  return (
    <g>
      <IsoExtrude plane="left" origin={origin} outline={DOCUMENT_OUTLINE} depth={3} material={material} />
      <IsoInset plane="left" origin={origin} rect={[4, 25, 12, 2]} kind="detail" />
      <IsoInset plane="left" origin={origin} rect={[4, 20, 18, 1.6]} kind="detail" fine />
      <IsoInset plane="left" origin={origin} rect={[4, 16, 15, 1.6]} kind="detail" fine />
      <IsoDisc plane="left" origin={origin} centre={[18.5, 7.5]} radius={4.2} kind="panel" />
    </g>
  );
}

// A command prompt (a chevron and a cursor bar) and a mouse pointer, drawn on a screen.
const CHEVRON: Pt[] = [[8, 18], [15, 14], [8, 10], [8, 12.2], [11.4, 14], [8, 15.8]];
const POINTER: Pt[] = [[15, 21], [15, 8], [18, 11], [20.4, 6.2], [22.2, 7], [19.8, 11.8], [24, 12]];

/** A monitor on its stand, showing a prompt (a session) or a pointer (desktop control). */
export function Monitor({ at, mark }: { at: Vec3; mark: "prompt" | "pointer" }) {
  const [x, y, z] = at;
  const [w, d, h] = [40, 4, 28];
  const face: Vec3 = [x, y + d, z + 6];
  return (
    <g>
      <IsoBox at={[x + 14, y - 6, z]} size={[12, 12, 2]} material="paper" />
      <IsoBox at={[x + 18, y - 1, z + 2]} size={[4, 3, 5]} material="paper" />
      <IsoBox at={[x, y, z + 6]} size={[w, d, h]} material="tint" />
      <IsoInset plane="left" origin={face} rect={[3, 3, w - 6, h - 6]} kind="panel" />
      {mark === "prompt" ? (
        <>
          <IsoFlat plane="left" origin={face} outline={CHEVRON} kind="screen" />
          <IsoInset plane="left" origin={face} rect={[17, 9.5, 9, 2]} kind="screen" />
        </>
      ) : (
        <IsoFlat plane="left" origin={face} outline={POINTER} kind="screen" />
      )}
    </g>
  );
}

/** Arrows standing upright for data moving out of or into a tray. */
export const ARROW_DOWN: Pt[] = [[6, 30], [6, 14], [0, 14], [10, 2], [20, 14], [14, 14], [14, 30]];
export const ARROW_UP: Pt[] = [[6, 0], [6, 16], [0, 16], [10, 28], [20, 16], [14, 16], [14, 0]];

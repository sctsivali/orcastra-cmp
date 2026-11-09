import * as React from "react";
import {
  alongNormal,
  circleOutline,
  clipConvex,
  ellipseAxes,
  fmt,
  onPlane,
  pointsAttr,
  screenLength,
  project,
  sideWall,
  signedArea,
  toScreen,
  type Face,
  type Plane,
  type Pt,
  type Vec3,
} from "./projection";

// Every fill and stroke comes from illustrations.css through these class names, never from a
// colour literal here: the look is a stylesheet decision, and both themes resolve their own
// tokens. scripts/checks/check-illustrations.mjs holds this.

/** paper: light body. tint: lavender accent. ink: the one solid hero piece. ghost: dashed outline. */
export type Material = "paper" | "tint" | "ink" | "ghost";

/**
 * Marks drawn on a face. detail: a solid mark on a light face. panel: a recessed screen or slot.
 * screen: a mark shown on a panel. cut: a mark on the solid hero face, which inverts with it.
 */
export type InsetKind = "detail" | "panel" | "screen" | "cut";

function faceClass(material: Material, face: Face, extra?: string): string {
  return `illo-face illo-${material} illo-${face}${extra ? ` ${extra}` : ""}`;
}

function Polygon({ points, className }: { points: readonly Vec3[]; className: string }) {
  return <polygon points={pointsAttr(project(points))} className={className} />;
}

function Line({ from, to, className }: { from: Vec3; to: Vec3; className: string }) {
  const [a, b] = [toScreen(from), toScreen(to)];
  return <line x1={fmt(a[0])} y1={fmt(a[1])} x2={fmt(b[0])} y2={fmt(b[1])} className={className} />;
}

/** The floor every scene stands on: one size everywhere, so objects share a scale. */
export function IsoGround({ half = 30, dashed = false }: { half?: number; dashed?: boolean }) {
  const corners: Vec3[] = [
    [-half, -half, 0],
    [half, -half, 0],
    [half, half, 0],
    [-half, half, 0],
  ];
  return <Polygon points={corners} className={`illo-ground${dashed ? " illo-dashed" : ""}`} />;
}

/** An axis-aligned block. `at` is its back-bottom corner (smallest x, y, z). */
export function IsoBox({
  at,
  size,
  material = "paper",
  open = false,
}: {
  at: Vec3;
  size: Vec3;
  material?: Material;
  /** Draws the top as a recess (a tray or a socket) instead of a lid. */
  open?: boolean;
}) {
  const [x, y, z] = at;
  const [w, d, h] = size;
  const top: Vec3[] = [
    [x, y, z + h],
    [x + w, y, z + h],
    [x + w, y + d, z + h],
    [x, y + d, z + h],
  ];
  const left: Vec3[] = [
    [x, y + d, z],
    [x + w, y + d, z],
    [x + w, y + d, z + h],
    [x, y + d, z + h],
  ];
  const right: Vec3[] = [
    [x + w, y, z],
    [x + w, y + d, z],
    [x + w, y + d, z + h],
    [x + w, y, z + h],
  ];
  if (material === "ghost") {
    return (
      <g>
        <Polygon points={top} className="illo-ghost" />
        <Polygon points={left} className="illo-ghost" />
        <Polygon points={right} className="illo-ghost" />
      </g>
    );
  }
  return (
    <g>
      <Polygon points={left} className={faceClass(material, "left")} />
      <Polygon points={right} className={faceClass(material, "right")} />
      <Polygon points={top} className={faceClass(material, "top")} />
      {open && <IsoRecess at={[x, y, z + h]} size={[w, d]} depth={h * 0.75} material={material} />}
    </g>
  );
}

// The inside of an open box. The two back walls and the floor are clipped to the opening, because
// the front walls and the rim hide everything else; drawing them whole would paint the floor over
// the front of the box.
function IsoRecess({ at, size, depth, material }: { at: Vec3; size: readonly [number, number]; depth: number; material: Material }) {
  const [x, y, z] = at;
  const [w, d] = size;
  const inset = Math.max(1.5, Math.min(w, d) * 0.1);
  const [x0, y0, x1, y1] = [x + inset, y + inset, x + w - inset, y + d - inset];
  const bottom = z - depth;
  const opening = project([
    [x0, y0, z],
    [x1, y0, z],
    [x1, y1, z],
    [x0, y1, z],
  ]);
  const inner: Array<[Vec3[], Face]> = [
    [[[x0, y0, bottom], [x1, y0, bottom], [x1, y1, bottom], [x0, y1, bottom]], "top"],
    [[[x0, y0, z], [x0, y1, z], [x0, y1, bottom], [x0, y0, bottom]], "right"],
    [[[x0, y0, z], [x1, y0, z], [x1, y0, bottom], [x0, y0, bottom]], "left"],
  ];
  return (
    <g>
      {inner.map(([points, face]) => {
        const visible = clipConvex(project(points), opening);
        return visible.length > 2 ? <polygon key={face} points={pointsAttr(visible)} className={faceClass(material, face)} /> : null;
      })}
    </g>
  );
}

/** Any flat shape drawn on a face (a play triangle, a check plate), in plane coordinates. */
export function IsoFlat({
  plane,
  origin,
  outline,
  kind = "detail",
  fine = false,
}: {
  plane: Plane;
  origin: Vec3;
  outline: readonly Pt[];
  kind?: InsetKind;
  /** Dropped at the small size, where it would only add noise. */
  fine?: boolean;
}) {
  const corners = outline.map(([u, v]) => onPlane(plane, origin, u, v));
  return <Polygon points={corners} className={`illo-${kind}${fine ? " illo-fine" : ""}`} />;
}

/** A rectangle drawn flat on a face: [u, v, width, height] in plane coordinates. */
export function IsoInset({
  rect,
  ...rest
}: {
  plane: Plane;
  origin: Vec3;
  rect: readonly [number, number, number, number];
  kind?: InsetKind;
  fine?: boolean;
}) {
  const [u, v, w, h] = rect;
  return (
    <IsoFlat
      {...rest}
      outline={[
        [u, v],
        [u + w, v],
        [u + w, v + h],
        [u, v + h],
      ]}
    />
  );
}

/** A stroked mark on a face (a check, a needle), in plane coordinates. */
export function IsoGlyph({ plane, origin, path, onInk = false }: { plane: Plane; origin: Vec3; path: readonly Pt[]; onInk?: boolean }) {
  const points = path.map(([u, v]) => onPlane(plane, origin, u, v));
  return <polyline points={pointsAttr(project(points))} className={onInk ? "illo-glyph illo-on-ink" : "illo-glyph"} />;
}

/** A circle drawn flat on a face (a fan, a dial, a lens). */
export function IsoDisc({
  plane,
  origin,
  centre,
  radius,
  kind = "panel",
  fine = false,
}: {
  plane: Plane;
  origin: Vec3;
  centre: Pt;
  radius: number;
  kind?: InsetKind;
  fine?: boolean;
}) {
  const ring = circleOutline(centre[0], centre[1], radius).map(([u, v]) => onPlane(plane, origin, u, v));
  return <Polygon points={ring} className={`illo-${kind}${fine ? " illo-fine" : ""}`} />;
}

/** An upright cylinder (a disk, a drum). `at` is the centre of its base. */
export function IsoCylinder({ at, radius, height, material = "paper" }: { at: Vec3; radius: number; height: number; material?: Material }) {
  const [x, y, z] = at;
  const { rx, ry } = ellipseAxes(radius);
  const [bx, by] = toScreen([x, y, z]);
  const [tx, ty] = toScreen([x, y, z + height]);
  const f = fmt;
  const body = `M${f(tx - rx)} ${f(ty)} L${f(bx - rx)} ${f(by)} A${f(rx)} ${f(ry)} 0 0 0 ${f(bx + rx)} ${f(by)} L${f(tx + rx)} ${f(ty)} A${f(rx)} ${f(ry)} 0 0 0 ${f(tx - rx)} ${f(ty)} Z`;
  const shade = `M${f(tx)} ${f(ty + ry)} L${f(bx)} ${f(by + ry)} A${f(rx)} ${f(ry)} 0 0 0 ${f(bx + rx)} ${f(by)} L${f(tx + rx)} ${f(ty)} A${f(rx)} ${f(ry)} 0 0 1 ${f(tx)} ${f(ty + ry)} Z`;
  const outline = `M${f(tx - rx)} ${f(ty)} L${f(bx - rx)} ${f(by)} A${f(rx)} ${f(ry)} 0 0 0 ${f(bx + rx)} ${f(by)} L${f(tx + rx)} ${f(ty)}`;
  if (material === "ghost") {
    return (
      <g>
        <path d={outline} className="illo-ghost" />
        <ellipse cx={f(tx)} cy={f(ty)} rx={f(rx)} ry={f(ry)} className="illo-ghost" />
      </g>
    );
  }
  return (
    <g>
      <path d={body} className={`illo-wall illo-${material} illo-left`} />
      <path d={shade} className={`illo-wall illo-${material} illo-right`} />
      <path d={outline} className={`illo-edge illo-${material}`} />
      <ellipse cx={f(tx)} cy={f(ty)} rx={f(rx)} ry={f(ry)} className={faceClass(material, "top")} />
    </g>
  );
}

/**
 * A flat 2D outline given thickness: a shield, a key, a document, a padlock body. The outline is
 * drawn on `plane` at `origin` and swept `depth` units away from the viewer. Walls facing away are
 * culled, and a crease line is drawn only where the outline turns sharply or meets the silhouette,
 * so curved walls stay one clean surface instead of a fan of slivers.
 */
export function IsoExtrude({
  plane,
  origin,
  outline,
  depth,
  material = "paper",
}: {
  plane: Plane;
  origin: Vec3;
  outline: readonly Pt[];
  depth: number;
  material?: Material;
}) {
  const ring = signedArea(outline) < 0 ? [...outline].reverse() : [...outline];
  const back = alongNormal(plane, origin, -depth);
  const front3 = ring.map(([u, v]) => onPlane(plane, origin, u, v));
  const back3 = ring.map(([u, v]) => onPlane(plane, back, u, v));
  const n = ring.length;
  const walls = ring.map((from, i) => ({ i, ...sideWall(plane, from, ring[(i + 1) % n]) }));

  // A vertex gets a crease when it sits on the silhouette (one wall visible, one not) or when the
  // outline turns by more than ~35 degrees there.
  const crease = ring.map((p, i) => {
    const prev = walls[(i - 1 + n) % n];
    const next = walls[i];
    if (prev.visible !== next.visible) return true;
    if (!prev.visible) return false;
    const a = ring[(i - 1 + n) % n];
    const b = ring[(i + 1) % n];
    const t1 = Math.atan2(p[1] - a[1], p[0] - a[0]);
    const t2 = Math.atan2(b[1] - p[1], b[0] - p[0]);
    const turn = Math.abs(Math.atan2(Math.sin(t2 - t1), Math.cos(t2 - t1)));
    return turn > 0.6;
  });

  const depthOf = (i: number) => {
    const [x1, y1, z1] = back3[i];
    const [x2, y2, z2] = back3[(i + 1) % n];
    return x1 + y1 + z1 + x2 + y2 + z2;
  };
  const visible = walls.filter((w) => w.visible).sort((a, b) => depthOf(a.i) - depthOf(b.i));

  if (material === "ghost") {
    return <Polygon points={front3} className="illo-ghost" />;
  }
  return (
    <g>
      {visible.map(({ i, face }) => {
        const j = (i + 1) % n;
        return (
          <g key={i}>
            <Polygon points={[front3[i], front3[j], back3[j], back3[i]]} className={`illo-wall illo-${material} illo-${face}`} />
            <Line from={back3[i]} to={back3[j]} className={`illo-edge illo-${material}`} />
            {crease[i] && <Line from={front3[i]} to={back3[i]} className={`illo-edge illo-${material}`} />}
            {crease[j] && <Line from={front3[j]} to={back3[j]} className={`illo-edge illo-${material}`} />}
          </g>
        );
      })}
      <Polygon points={front3} className={faceClass(material, plane)} />
    </g>
  );
}

/** A dashed link between objects, in world coordinates. */
export function IsoLink({ path, fine = false }: { path: readonly Vec3[]; fine?: boolean }) {
  return <polyline points={pointsAttr(project(path))} className={`illo-link${fine ? " illo-fine" : ""}`} />;
}

/** The orcastra.io diamond mark, upright in screen space, at a world point. */
export function IsoMark({ at, size = 4 }: { at: Vec3; size?: number }) {
  const [cx, cy] = toScreen(at);
  const pts: Pt[] = [
    [cx, cy - size],
    [cx + size, cy],
    [cx, cy + size],
    [cx - size, cy],
  ];
  return <polygon points={pointsAttr(pts)} className="illo-mark" />;
}

/** A round head for a person token; a sphere projects to a circle. */
export function IsoBall({ at, radius, material = "paper" }: { at: Vec3; radius: number; material?: Material }) {
  const [cx, cy] = toScreen(at);
  return <circle cx={fmt(cx)} cy={fmt(cy)} r={fmt(screenLength(radius))} className={faceClass(material, "top")} />;
}

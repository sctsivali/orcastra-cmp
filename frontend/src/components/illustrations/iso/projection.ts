// True isometric projection (30 degrees), the same angle as the hexagon marks on orcastra.io.
// World axes: +x runs to the lower right, +y to the lower left, +z straight up. One world unit
// is one viewBox unit along each axis, so every scene shares one scale on the 160x120 canvas.

export type Vec3 = readonly [number, number, number];
export type Pt = readonly [number, number];

/** Which visible face a polygon belongs to; it picks the face tone. */
export type Face = "top" | "left" | "right";

/** A vertical plane facing the viewer's left (y = const), its right (x = const), or the ground. */
export type Plane = "left" | "right" | "top";

export const CANVAS = { width: 160, height: 120 } as const;

// The ground plate's centre sits a little below the canvas middle so objects rising from it
// stay inside the frame. SCALE sets how big objects are against the plate: at 1.25 the default
// plate (half 30) spans x 15..145, y 38.5..113.5 and the objects, not the floor, fill the frame.
const ORIGIN: Pt = [80, 76];
const SCALE = 1.25;
const COS30 = Math.sqrt(3) / 2;

export function toScreen([x, y, z]: Vec3): Pt {
  return [ORIGIN[0] + (x - y) * COS30 * SCALE, ORIGIN[1] + ((x + y) * 0.5 - z) * SCALE];
}

/** A length in world units as screen units (a ball's radius). */
export function screenLength(n: number): number {
  return n * SCALE;
}

// note: two decimals keep the markup small without visible drift at 3x zoom. `-0` is folded so
// server and client render identical strings.
export function fmt(n: number): string {
  const rounded = Math.round(n * 100) / 100;
  return String(Object.is(rounded, -0) ? 0 : rounded);
}

export function pointsAttr(points: readonly Pt[]): string {
  return points.map(([a, b]) => `${fmt(a)},${fmt(b)}`).join(" ");
}

export function project(points: readonly Vec3[]): Pt[] {
  return points.map(toScreen);
}

/**
 * A point on a plane through `origin`, addressed in that plane's own 2D coordinates.
 * left plane: u along +x, v up. right plane: u along -y (so u still reads left to right on
 * screen), v up. top plane: u along +x, v along +y.
 */
export function onPlane(plane: Plane, origin: Vec3, u: number, v: number): Vec3 {
  const [x, y, z] = origin;
  if (plane === "left") return [x + u, y, z + v];
  if (plane === "right") return [x, y - u, z + v];
  return [x + u, y + v, z];
}

/** Offset along the plane's outward normal (positive moves toward the viewer). */
export function alongNormal(plane: Plane, origin: Vec3, distance: number): Vec3 {
  const [x, y, z] = origin;
  if (plane === "left") return [x, y + distance, z];
  if (plane === "right") return [x + distance, y, z];
  return [x, y, z + distance];
}

/** The face tone a plane's front side takes. */
export function planeFace(plane: Plane): Face {
  return plane;
}

/** Signed area in the plane's own coordinates; positive means counter-clockwise. */
export function signedArea(outline: readonly Pt[]): number {
  let sum = 0;
  for (let i = 0; i < outline.length; i += 1) {
    const [u1, v1] = outline[i];
    const [u2, v2] = outline[(i + 1) % outline.length];
    sum += u1 * v2 - u2 * v1;
  }
  return sum / 2;
}

/**
 * For an edge of a counter-clockwise outline on `plane`, whether the side wall it sweeps when
 * extruded faces the viewer, and which face tone it takes. The viewer looks down (1, 1, 1), so a
 * wall is visible when its world normal has a positive component sum.
 */
export function sideWall(plane: Plane, from: Pt, to: Pt): { visible: boolean; face: Face } {
  const du = to[0] - from[0];
  const dv = to[1] - from[1];
  // Outward normal of a counter-clockwise edge, in plane coordinates.
  const nu = dv;
  const nv = -du;
  if (plane === "left") {
    // world normal (nu, 0, nv)
    return { visible: nu + nv > 1e-9, face: nv >= nu ? "top" : "right" };
  }
  if (plane === "right") {
    // world normal (0, -nu, nv)
    return { visible: -nu + nv > 1e-9, face: nv >= -nu ? "top" : "left" };
  }
  // top plane, extruded downward: world normal (nu, nv, 0)
  return { visible: nu + nv > 1e-9, face: nu >= nv ? "right" : "left" };
}

/** Points of a circle on a plane, as a closed polyline (a projected circle is an ellipse). */
export function circleOnPlane(plane: Plane, centre: Vec3, radius: number, segments = 28): Vec3[] {
  const out: Vec3[] = [];
  for (let i = 0; i < segments; i += 1) {
    const t = (i / segments) * Math.PI * 2;
    out.push(onPlane(plane, centre, Math.cos(t) * radius, Math.sin(t) * radius));
  }
  return out;
}

/** 2D outline of a circle in plane coordinates, counter-clockwise. */
export function circleOutline(cu: number, cv: number, radius: number, segments = 28): Pt[] {
  const out: Pt[] = [];
  for (let i = 0; i < segments; i += 1) {
    const t = (i / segments) * Math.PI * 2;
    out.push([cu + Math.cos(t) * radius, cv + Math.sin(t) * radius]);
  }
  return out;
}

/**
 * The part of `subject` inside the convex polygon `clip` (Sutherland-Hodgman), in screen space.
 * Used to show only what can be seen through an opening, without an SVG clipPath and the
 * document-unique id it would need.
 */
export function clipConvex(subject: readonly Pt[], clip: readonly Pt[]): Pt[] {
  const orient = Math.sign(signedArea(clip)) || 1;
  let output: Pt[] = [...subject];
  for (let i = 0; i < clip.length && output.length > 0; i += 1) {
    const a = clip[i];
    const b = clip[(i + 1) % clip.length];
    const side = (p: Pt) => orient * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]));
    const input = output;
    output = [];
    for (let j = 0; j < input.length; j += 1) {
      const p = input[j];
      const q = input[(j + 1) % input.length];
      const sp = side(p);
      const sq = side(q);
      if (sp >= 0) output.push(p);
      if ((sp >= 0) !== (sq >= 0)) {
        const t = sp / (sp - sq);
        output.push([p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t]);
      }
    }
  }
  return output;
}

/** An outline scaled about its origin and shifted, for reusing one shape at another size. */
export function scaleOutline(outline: readonly Pt[], k: number, du = 0, dv = 0): Pt[] {
  return outline.map(([u, v]) => [u * k + du, v * k + dv]);
}

/** Screen-space half axes of a horizontal circle of `radius` (an isometric ellipse). */
export function ellipseAxes(radius: number): { rx: number; ry: number } {
  return { rx: radius * Math.SQRT2 * COS30 * SCALE, ry: radius * Math.SQRT2 * 0.5 * SCALE };
}

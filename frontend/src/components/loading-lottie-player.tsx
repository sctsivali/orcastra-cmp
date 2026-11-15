"use client";

import * as React from "react";
import Lottie from "lottie-react";
// important: keep this import here, not in loading-lottie.tsx. At module scope in that
// eagerly imported file it would pin about 113KB of animation JSON into a chunk shared by
// many routes. Reachable only through the dynamic() in loading-lottie.tsx, webpack
// co-locates it with the lottie-react chunk instead.
import loadingAnimation from "../../public/loading-lotttie-orca.json";
import { PersonalizationContext } from "@/components/personalization-provider";
import { accentHueShift, currentAccentHueShift, isHexColor, rotateRgbHue } from "@/lib/personalization-colors";

type Json = unknown;

/**
 * The animation with every static fill and stroke colour turned by `degrees`. The orca is drawn
 * in the shipped accent's purples, so a deployment branded another colour sees it in that hue
 * family; 0 returns the original object untouched.
 */
function recoloured(animation: Json, degrees: number): Json {
  if (!degrees) return animation;
  const walk = (node: Json): Json => {
    if (Array.isArray(node)) return node.map(walk);
    if (!node || typeof node !== "object") return node;
    const record = node as Record<string, Json>;
    const out: Record<string, Json> = {};
    for (const [key, value] of Object.entries(record)) out[key] = walk(value);
    const color = record.c as { k?: Json } | undefined;
    if ((record.ty === "fl" || record.ty === "st") && color && Array.isArray(color.k) &&
        color.k.length >= 3 && color.k.every((v) => typeof v === "number")) {
      const [r, g, b, ...alpha] = color.k as number[];
      out.c = { ...(out.c as object), k: [...rotateRgbHue([r, g, b], degrees), ...alpha] };
    }
    return out;
  };
  return walk(animation);
}

const cache = new Map<number, Json>();

export default function LoadingLottiePlayer() {
  // The accent the provider holds, so a loader already on screen turns when the organization's
  // colours arrive. Outside the provider (none today) it falls back to what the root carries,
  // which the pre-paint script set from the hint cookie.
  const personalization = React.useContext(PersonalizationContext);
  const accent = personalization?.settings.colors.accentColor;
  const shift = Math.round(isHexColor(accent) ? accentHueShift(accent) : currentAccentHueShift());
  const data = React.useMemo(() => {
    if (!cache.has(shift)) cache.set(shift, recoloured(loadingAnimation, shift));
    return cache.get(shift);
  }, [shift]);
  return <Lottie animationData={data as object} loop autoplay />;
}

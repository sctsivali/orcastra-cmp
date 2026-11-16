"use client";

import Lottie from "lottie-react";
// important: keep this import here, not in loading-lottie.tsx. At module scope in that
// eagerly imported file it would pin about 113KB of animation JSON into a chunk shared by
// many routes. Reachable only through the dynamic() in loading-lottie.tsx, webpack
// co-locates it with the lottie-react chunk instead.
import loadingAnimation from "../../public/loading-lotttie-orca.json";

// The orca is the Orcastra mark and keeps the purples it is drawn in, under any accent and
// any seller's branding. Card illustrations follow the accent; the mark does not.
export default function LoadingLottiePlayer() {
  return <Lottie animationData={loadingAnimation} loop autoplay />;
}

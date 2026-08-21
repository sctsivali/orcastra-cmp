"use client";

import Lottie from "lottie-react";
// important: this import lives here rather than in loading-lottie.tsx. At module scope in the
// eagerly-imported file it pinned 113KB of animation JSON into a shared chunk carried by 23
// routes, whether or not the route ever rendered a loader. Reachable only through the
// dynamic() in loading-lottie.tsx, webpack co-locates it with the lottie-react chunk instead.
import loadingAnimation from "../../public/loading-lotttie-orca.json";

export default function LoadingLottiePlayer() {
  return <Lottie animationData={loadingAnimation} loop autoplay />;
}

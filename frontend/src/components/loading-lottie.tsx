"use client";

import Lottie from "lottie-react";
import loadingAnimation from "../../public/loading-lotttie-orca.json";
import { cn } from "@/lib/utils";

interface LoadingLottieProps {
  /**
   * Size of the animation container in pixels
   * @default 80
   */
  size?: number;
  /**
   * Additional CSS classes for the container
   */
  className?: string;
  /**
   * Whether to show centered on screen
   * @default false
   */
  centered?: boolean;
}

/**
 * Reusable Lottie loading animation component
 * Uses the custom Orca loading animation from public/loading-lotttie-orca.json
 */
export function LoadingLottie({ 
  size = 80, 
  className,
  centered = false 
}: LoadingLottieProps) {
  const animation = (
    <div
      className={cn(className)}
      style={{
        width: size,
        height: size,
      }}
    >
      <Lottie
        animationData={loadingAnimation}
        loop={true}
        autoplay={true}
      />
    </div>
  );

  if (centered) {
    return (
      <div className="flex items-center justify-center h-screen">
        {animation}
      </div>
    );
  }

  return animation;
}

/**
 * Small inline loading animation (for buttons, inline elements)
 */
export function LoadingLottieSmall({ className }: { className?: string }) {
  return <LoadingLottie size={20} className={className} />;
}

/**
 * Medium loading animation (for cards, sections)
 */
export function LoadingLottieMedium({ className }: { className?: string }) {
  return <LoadingLottie size={40} className={className} />;
}

/**
 * Large centered loading animation (for full page loading)
 */
export function LoadingLottiePage() {
  return <LoadingLottie size={100} centered />;
}

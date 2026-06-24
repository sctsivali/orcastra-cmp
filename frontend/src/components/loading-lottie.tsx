"use client";

import dynamic from "next/dynamic";
import loadingAnimation from "../../public/loading-lotttie-orca.json";
import { Progress } from "@/components/ui/progress";
import { useSimulatedProgress, LOADING_MESSAGES } from "@/hooks/use-loading-progress";
import { useSharedRouteProgress } from "@/hooks/use-route-loading-progress";

// Lazy-load Lottie renderer to reduce initial JS bundle (~80KB savings)
const Lottie = dynamic(() => import("lottie-react"), { ssr: false });

// important: the orca Lottie is reserved for page/route navigation only. Inline and modal
// loaders (buttons, dialogs, cells) use the lightweight <Spinner> (components/ui/spinner.tsx).

interface LoadingLottiePageEnhancedProps {
  /**
   * Loading context for status messages
   * @default "data"
   */
  context?: string;
  /**
   * Custom loading phases
   */
  phases?: Array<{ id: string; label: string }>;
  /**
   * Custom status message (overrides automatic)
   */
  statusMessage?: string;
  /**
   * Custom progress value (0-100)
   */
  progress?: number;
  /**
   * Title text shown above the animation
   */
  title?: string;
}

/**
 * Presentational full-page loader: whale animation + progress bar + status.
 * Progress/message are supplied by the caller so the source hook can vary.
 */
function LoadingLottieView({
  progress,
  statusMessage,
  title,
}: {
  progress: number;
  statusMessage: string;
  title?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] bg-background">
      {/* Title */}
      {title && (
        <h2 className="text-lg font-medium text-foreground mb-4 animate-pulse">
          {title}
        </h2>
      )}

      {/* Lottie Animation */}
      <div className="w-24 h-24 mb-6">
        <Lottie
          animationData={loadingAnimation}
          loop={true}
          autoplay={true}
        />
      </div>

      {/* Progress Container */}
      <div className="w-72 space-y-3">
        {/* Progress Bar with shimmer effect */}
        <div className="relative px-4">
          <Progress
            value={progress}
            className="h-2.5 bg-secondary/50"
            animated
          />
          {/* Progress percentage */}
          <div className="absolute -right-4 top-1/2 -translate-y-1/2 text-xs text-foreground/70 font-mono w-8 text-right">
            {Math.round(progress)}%
          </div>
        </div>

        {/* Status Message - dynamic rotating */}
        <div className="text-center min-h-[1.5rem]">
          <p className="text-sm text-foreground/80 transition-opacity duration-500">
            {statusMessage || LOADING_MESSAGES.initializing}
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * Enhanced loading page with its own per-instance simulated progress.
 * Used where the loader is independent of route transitions (ex; LoadingOverlay
 * for form submits) or where the caller drives progress/message explicitly.
 */
export function LoadingLottiePageEnhanced({
  context = "data",
  phases,
  statusMessage: customStatusMessage,
  progress: customProgress,
  title,
}: LoadingLottiePageEnhancedProps) {
  // Use simulated progress hook - always loading when this component is shown
  const loadingState = useSimulatedProgress(true, { context, phases });

  // Use custom values if provided, otherwise use simulated values
  const progress = customProgress ?? loadingState.progress;
  const statusMessage = customStatusMessage ?? loadingState.statusMessage;

  return (
    <LoadingLottieView progress={progress} statusMessage={statusMessage} title={title} />
  );
}

/**
 * Full-page loader for route transitions AND page data fetches. Progress is
 * shared across loader mounts (see useSharedRouteProgress), so the route
 * transition loader and the page's own loader read as one continuous whale.
 */
export function LoadingLottiePage({ title }: { title?: string }) {
  const { progress, statusMessage } = useSharedRouteProgress();
  return (
    <LoadingLottieView progress={progress} statusMessage={statusMessage} title={title} />
  );
}

interface LoadingOverlayProps {
  /**
   * Whether the overlay is visible
   */
  isVisible: boolean;
  /**
   * Custom status message
   */
  statusMessage?: string;
  /**
   * Custom progress value (0-100)
   */
  progress?: number;
  /**
   * Title text
   */
  title?: string;
}

/**
 * Full-screen loading overlay with progress.
 * Useful for blocking operations like form submissions.
 */
export function LoadingOverlay({
  isVisible,
  statusMessage,
  progress,
  title,
}: LoadingOverlayProps) {
  if (!isVisible) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm">
      <LoadingLottiePageEnhanced
        statusMessage={statusMessage}
        progress={progress}
        title={title}
      />
    </div>
  );
}

"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { cn } from "@/lib/utils";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { 
  useSimulatedProgress,
  LOADING_MESSAGES,
  DYNAMIC_MESSAGES,
} from "@/hooks/use-loading-progress";

// The renderer and the animation JSON load together from loading-lottie-player, so neither
// reaches a route bundle that never renders a loader. The fallback shows while this chunk
// loads on a cold cache; an empty box there would look like a broken loader.
const LottiePlayer = dynamic(() => import("./loading-lottie-player"), {
  ssr: false,
  loading: () => <Skeleton className="h-full w-full rounded-full" />,
});

interface LoadingLottieProps {
  /** Container size in pixels. */
  size?: number;
  className?: string;
  /** Centre the animation on the screen. */
  centered?: boolean;
}

/** The Orca loading animation (public/loading-lotttie-orca.json). */
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
      <LottiePlayer />
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

/** For buttons and inline elements. */
export function LoadingLottieSmall({ className }: { className?: string }) {
  return <LoadingLottie size={20} className={className} />;
}

/** For cards and sections. */
export function LoadingLottieMedium({ className }: { className?: string }) {
  return <LoadingLottie size={40} className={className} />;
}

interface LoadingLottiePageEnhancedProps {
  /** Picks the set of status messages. */
  context?: string;
  phases?: Array<{ id: string; label: string }>;
  /** Overrides the simulated status message. */
  statusMessage?: string;
  /** 0 to 100. Simulated when absent. */
  progress?: number;
  /** Shown above the animation. */
  title?: string;
}

/** Full-page loader with a progress bar and rotating status messages. */
export function LoadingLottiePageEnhanced({
  context = "data",
  phases,
  statusMessage: customStatusMessage,
  progress: customProgress,
  title,
}: LoadingLottiePageEnhancedProps) {
  // Always loading while this component is shown.
  const loadingState = useSimulatedProgress(true, { context, phases });
  
  const progress = customProgress ?? loadingState.progress;
  const statusMessage = customStatusMessage ?? loadingState.statusMessage;
  
  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] bg-background">
      {title && (
        <h2 className="text-lg font-medium text-foreground mb-4 animate-pulse">
          {title}
        </h2>
      )}
      
      <div className="w-24 h-24 mb-6">
        <LottiePlayer />
      </div>
      
      <div className="w-72 space-y-3">
        <div className="relative px-4">
          <Progress 
            value={progress} 
            className="h-2.5 bg-secondary/50"
            animated
          />
          <div className="absolute -right-4 top-1/2 -translate-y-1/2 text-xs text-foreground/70 font-mono w-8 text-right">
            {Math.round(progress)}%
          </div>
        </div>
        
        <div className="text-center min-h-[1.5rem]">
          <p className="text-sm text-foreground/80 transition-opacity duration-500">
            {statusMessage || LOADING_MESSAGES.initializing}
          </p>
        </div>
      </div>
    </div>
  );
}

/** Full-page loader with the default settings. */
export function LoadingLottiePage() {
  return <LoadingLottiePageEnhanced />;
}

interface LoadingLottieMediumEnhancedProps {
  className?: string;
  /** Overrides the rotating message. */
  statusMessage?: string;
  /** 0 to 100. Simulated when absent. */
  progress?: number;
  showProgress?: boolean;
}

function getRandomFromArray<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

/** Medium loader for cards and sections, with optional progress and rotating messages. */
export function LoadingLottieMediumEnhanced({
  className,
  statusMessage,
  progress,
  showProgress = true,
}: LoadingLottieMediumEnhancedProps) {
  const [simulatedProgress, setSimulatedProgress] = React.useState(0);
  const [message, setMessage] = React.useState(() => 
    getRandomFromArray(DYNAMIC_MESSAGES.fetching)
  );
  const [currentPhase, setCurrentPhase] = React.useState<keyof typeof DYNAMIC_MESSAGES>("fetching");
  const messageRotationRef = React.useRef<number | null>(null);
  
  const getPhase = React.useCallback((prog: number): keyof typeof DYNAMIC_MESSAGES => {
    if (prog < 30) return "fetching";
    if (prog < 60) return "processing";
    if (prog < 90) return "rendering";
    return "almostDone";
  }, []);
  
  React.useEffect(() => {
    if (progress !== undefined) return;
    
    const rotateMessage = () => {
      setMessage(getRandomFromArray(DYNAMIC_MESSAGES[currentPhase]));
    };
    
    messageRotationRef.current = window.setInterval(rotateMessage, 2500);
    
    return () => {
      if (messageRotationRef.current) {
        clearInterval(messageRotationRef.current);
      }
    };
  }, [currentPhase, progress]);
  
  React.useEffect(() => {
    if (progress !== undefined) return;
    
    const startTime = Date.now();
    let lastPhase: keyof typeof DYNAMIC_MESSAGES = "fetching";
    
    const animate = () => {
      const elapsed = Date.now() - startTime;
      const newProgress = Math.min(95, (1 - Math.exp(-elapsed / 1600)) * 100);
      setSimulatedProgress(newProgress);
      
      const newPhase = getPhase(newProgress);
      if (newPhase !== lastPhase) {
        lastPhase = newPhase;
        setCurrentPhase(newPhase);
        setMessage(getRandomFromArray(DYNAMIC_MESSAGES[newPhase]));
      }
      
      if (newProgress < 95) {
        requestAnimationFrame(animate);
      }
    };
    
    const frame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frame);
  }, [progress, getPhase]);
  
  const displayProgress = progress ?? simulatedProgress;
  const displayMessage = statusMessage ?? message;
  
  return (
    <div className={cn("flex flex-col items-center gap-2 py-4", className)}>
      <LoadingLottie size={40} />
      
      {showProgress && (
        <div className="w-36 space-y-1.5">
          <Progress value={displayProgress} className="h-1.5" animated />
          <p className="text-xs text-center text-muted-foreground truncate transition-opacity duration-500">
            {displayMessage}
          </p>
        </div>
      )}
    </div>
  );
}

interface LoadingOverlayProps {
  isVisible: boolean;
  statusMessage?: string;
  /** 0 to 100. Simulated when absent. */
  progress?: number;
  title?: string;
}

/** Full-screen loading overlay for blocking operations such as form submissions. */
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

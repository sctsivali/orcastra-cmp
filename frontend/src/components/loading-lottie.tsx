"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import loadingAnimation from "../../public/loading-lotttie-orca.json";
import { cn } from "@/lib/utils";
import { Progress } from "@/components/ui/progress";
import { 
  useSimulatedProgress,
  LOADING_MESSAGES,
  DYNAMIC_MESSAGES,
} from "@/hooks/use-loading-progress";

// Lazy-load Lottie renderer to reduce initial JS bundle (~80KB savings)
const Lottie = dynamic(() => import("lottie-react"), { ssr: false });

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
 * Enhanced loading page component with progress bar and status
 * Shows animated progress bar with dynamic rotating status messages
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
 * Enhanced full-page loading with progress - backward compatible
 */
export function LoadingLottiePage() {
  return <LoadingLottiePageEnhanced />;
}

interface LoadingLottieMediumEnhancedProps {
  /**
   * Additional CSS classes
   */
  className?: string;
  /**
   * Custom status message
   */
  statusMessage?: string;
  /**
   * Custom progress value (0-100)
   */
  progress?: number;
  /**
   * Whether to show progress bar
   * @default true
   */
  showProgress?: boolean;
}

// Helper to get random message from array
function getRandomFromArray<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

/**
 * Enhanced medium loading component with optional progress
 * Suitable for cards and sections - with dynamic rotating messages
 */
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
  
  // Get phase based on progress
  const getPhase = React.useCallback((prog: number): keyof typeof DYNAMIC_MESSAGES => {
    if (prog < 30) return "fetching";
    if (prog < 60) return "processing";
    if (prog < 90) return "rendering";
    return "almostDone";
  }, []);
  
  // Rotate messages every 2.5 seconds
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
  
  // Simulate progress if not provided
  React.useEffect(() => {
    if (progress !== undefined) return;
    
    const startTime = Date.now();
    let lastPhase: keyof typeof DYNAMIC_MESSAGES = "fetching";
    
    const animate = () => {
      const elapsed = Date.now() - startTime;
      const newProgress = Math.min(95, (1 - Math.exp(-elapsed / 1600)) * 100);
      setSimulatedProgress(newProgress);
      
      // Check if phase changed
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
 * Full-screen loading overlay with progress
 * Useful for blocking operations like form submissions
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

/**
 * Loading Progress Hook & Context
 * Provides loading state management with progress tracking and status messages
 * Uses React hooks for local state - lightweight and dependency-free
 */

import * as React from "react";

export interface LoadingPhase {
  id: string;
  label: string;
  status: "pending" | "in-progress" | "completed" | "error";
  startTime?: number;
  endTime?: number;
}

export interface LoadingState {
  isLoading: boolean;
  progress: number; // 0-100
  statusMessage: string;
  currentPhase: string;
  phases: LoadingPhase[];
}

// Dynamic loading messages - 5-10 variations per phase to keep it interesting
export const DYNAMIC_MESSAGES = {
  // Phase 1: Initializing (0-15%)
  initializing: [
    "Initializing dashboard...",
    "Waking up the system...",
    "Getting things ready...",
    "Starting up...",
    "Preparing environment...",
    "Booting up services...",
    "Setting things in motion...",
    "This is the part where magic happens",
  ],
  
  // Phase 2: Connecting (15-30%)
  connecting: [
    "Connecting to backend services...",
    "Establishing connection...",
    "Reaching out to servers...",
    "Opening secure channels...",
    "Syncing with backend...",
    "Handshaking with services...",
    "Building connections...",
    "Please wait, the cloud is thinking",
  ],
  
  // Phase 3: Fetching (30-50%)
  fetching: [
    "Fetching data from servers...",
    "Retrieving information...",
    "Gathering data...",
    "Pulling latest records...",
    "Loading resources...",
    "Collecting data points...",
    "Requesting data...",
    "Downloading content...",
    "Yes, it's doing something important",
    "Still faster than explaining cloud to management",
  ],
  
  // Phase 4: Processing (50-70%)
  processing: [
    "Processing received data...",
    "Crunching the numbers...",
    "Analyzing information...",
    "Organizing data...",
    "Sorting things out...",
    "Making sense of it all...",
    "Transforming data...",
    "Computing results...",
    "Optimizing things you'll never see",
    "Loading… because physics exists",
  ],
  
  // Phase 5: Rendering (70-90%)
  rendering: [
    "Preparing display...",
    "Building the view...",
    "Assembling components...",
    "Painting the interface...",
    "Laying out elements...",
    "Constructing the page...",
    "Rendering visuals...",
    "Clouds move slowly when they're being careful",
  ],
  
  // Phase 6: Almost done (90-100%)
  almostDone: [
    "Almost done...",
    "Nearly there...",
    "Just a moment...",
    "Finishing up...",
    "Final touches...",
    "Wrapping things up...",
    "One moment please...",
    "Almost ready...",
    "Hang tight...",
    "Just about done...",
    "Almost done. Probably.",
  ],
  
  // Complete
  complete: [
    "Complete!",
    "All done!",
    "Ready!",
    "Loaded!",
  ],
};

// Helper to get random message from array (with seed for consistency during same render)
function getRandomMessage(messages: string[], seed?: number): string {
  const index = seed !== undefined 
    ? Math.floor(seed) % messages.length 
    : Math.floor(Math.random() * messages.length);
  return messages[index];
}

// Legacy static messages (for backward compatibility)
export const LOADING_MESSAGES = {
  initializing: DYNAMIC_MESSAGES.initializing[0],
  connecting: DYNAMIC_MESSAGES.connecting[0],
  fetchingData: DYNAMIC_MESSAGES.fetching[0],
  processingData: DYNAMIC_MESSAGES.processing[0],
  renderingContent: DYNAMIC_MESSAGES.rendering[0],
  almostDone: DYNAMIC_MESSAGES.almostDone[0],
  finalizing: "Finalizing...",
  complete: DYNAMIC_MESSAGES.complete[0],
  
  // Context-specific (legacy)
  fetchingVMs: "Loading virtual machines...",
  processingVMs: "Processing VM information...",
  fetchingNetworks: "Loading network configurations...",
  processingNetworks: "Analyzing network topology...",
  fetchingImages: "Loading container images...",
  processingImages: "Processing image metadata...",
  fetchingProfiles: "Loading profiles...",
  processingProfiles: "Processing profile configurations...",
  fetchingStorage: "Loading storage pools...",
  processingStorage: "Analyzing storage configurations...",
  fetchingClusters: "Loading cluster information...",
  processingClusters: "Processing cluster data...",
  fetchingServer: "Loading server information...",
  processingServer: "Processing server details...",
  authenticating: "Authenticating session...",
};

// Predefined loading phases for different page types
export const LOADING_PHASES = {
  // Generic data page loading phases
  dataPage: [
    { id: "init", label: "Initializing" },
    { id: "fetch", label: "Fetching Data" },
    { id: "process", label: "Processing" },
    { id: "render", label: "Rendering" },
  ],
  
  // VM page specific
  vms: [
    { id: "init", label: "Initializing" },
    { id: "fetch", label: "Fetching VMs" },
    { id: "nodes", label: "Loading Clusters" },
    { id: "process", label: "Processing" },
    { id: "render", label: "Rendering" },
  ],
  
  // Network page specific
  networks: [
    { id: "init", label: "Initializing" },
    { id: "fetch", label: "Fetching Networks" },
    { id: "topology", label: "Loading Topology" },
    { id: "render", label: "Rendering" },
  ],
  
  // Dashboard
  dashboard: [
    { id: "init", label: "Initializing" },
    { id: "health", label: "Health Check" },
    { id: "stats", label: "Loading Stats" },
    { id: "nodes", label: "Loading Clusters" },
    { id: "render", label: "Rendering" },
  ],
};

// Initial loading state
const initialState: LoadingState = {
  isLoading: false,
  progress: 0,
  statusMessage: "",
  currentPhase: "",
  phases: [],
};

/**
 * Custom hook for managing loading progress state
 * Provides local state management without external dependencies
 */
export function useLoadingProgress(
  initialPhases?: Array<{ id: string; label: string }>
) {
  const [state, setState] = React.useState<LoadingState>(() => ({
    ...initialState,
    phases: initialPhases?.map((p, i) => ({
      ...p,
      status: i === 0 ? "in-progress" : "pending",
    })) || [],
  }));
  
  const startLoading = React.useCallback(
    (phases: Array<{ id: string; label: string }>) => {
      setState({
        isLoading: true,
        progress: 0,
        statusMessage: LOADING_MESSAGES.initializing,
        currentPhase: phases[0]?.id || "",
        phases: phases.map((p, index) => ({
          ...p,
          status: index === 0 ? "in-progress" : "pending",
          startTime: index === 0 ? Date.now() : undefined,
        })),
      });
    },
    []
  );
  
  const updateProgress = React.useCallback(
    (progress: number, statusMessage?: string) => {
      setState((prev) => ({
        ...prev,
        progress: Math.min(100, Math.max(0, progress)),
        statusMessage: statusMessage ?? prev.statusMessage,
      }));
    },
    []
  );
  
  const setPhase = React.useCallback(
    (phaseId: string, status: LoadingPhase["status"] = "in-progress") => {
      setState((prev) => {
        const phases = prev.phases.map((p) => {
          if (p.id === phaseId) {
            return {
              ...p,
              status,
              startTime: status === "in-progress" ? Date.now() : p.startTime,
            };
          }
          return p;
        });
        
        const completedPhases = phases.filter((p) => p.status === "completed").length;
        const progress = (completedPhases / phases.length) * 100;
        const phaseLabel = phases.find((p) => p.id === phaseId)?.label || "";
        
        return {
          ...prev,
          phases,
          currentPhase: phaseId,
          progress,
          statusMessage: `${phaseLabel}...`,
        };
      });
    },
    []
  );
  
  const completePhase = React.useCallback((phaseId: string) => {
    setState((prev) => {
      const phases = prev.phases.map((p) => {
        if (p.id === phaseId) {
          return { ...p, status: "completed" as const, endTime: Date.now() };
        }
        return p;
      });
      
      const completedPhases = phases.filter((p) => p.status === "completed").length;
      const progress = (completedPhases / phases.length) * 100;
      
      const nextPhase = phases.find((p) => p.status === "pending");
      const newPhases = nextPhase
        ? phases.map((p) =>
            p.id === nextPhase.id
              ? { ...p, status: "in-progress" as const, startTime: Date.now() }
              : p
          )
        : phases;
      
      const statusMessage = nextPhase
        ? `${nextPhase.label}...`
        : LOADING_MESSAGES.finalizing;
      
      return {
        ...prev,
        phases: newPhases,
        currentPhase: nextPhase?.id || prev.currentPhase,
        progress,
        statusMessage,
      };
    });
  }, []);
  
  const finishLoading = React.useCallback(() => {
    setState((prev) => ({
      ...prev,
      isLoading: false,
      progress: 100,
      statusMessage: LOADING_MESSAGES.complete,
      phases: prev.phases.map((p) => ({
        ...p,
        status: "completed" as const,
        endTime: p.endTime || Date.now(),
      })),
    }));
  }, []);
  
  const reset = React.useCallback(() => {
    setState(initialState);
  }, []);
  
  return {
    ...state,
    startLoading,
    updateProgress,
    setPhase,
    completePhase,
    finishLoading,
    reset,
  };
}

/**
 * Hook to simulate progressive loading for data fetching
 * Automatically updates progress based on time elapsed
 * Returns progress value and status message for display
 * Uses dynamic rotating messages to keep it interesting
 */
export function useSimulatedProgress(
  isLoading: boolean,
  options?: {
    minDuration?: number; // Minimum animation duration in ms
    phases?: Array<{ id: string; label: string }>;
    context?: string; // Loading context for status messages
    messageRotationInterval?: number; // How often to rotate messages (ms)
  }
) {
  const { minDuration = 800, phases, messageRotationInterval = 2500 } = options || {};
  
  const [progress, setProgress] = React.useState(0);
  const [statusMessage, setStatusMessage] = React.useState(
    getRandomMessage(DYNAMIC_MESSAGES.initializing)
  );
  const [currentPhases, setCurrentPhases] = React.useState<LoadingPhase[]>([]);
  const [currentPhaseKey, setCurrentPhaseKey] = React.useState<keyof typeof DYNAMIC_MESSAGES>("initializing");
  const messageIndexRef = React.useRef(0);
  
  // Get current phase based on progress
  const getPhaseKey = React.useCallback((prog: number): keyof typeof DYNAMIC_MESSAGES => {
    if (prog < 15) return "initializing";
    if (prog < 30) return "connecting";
    if (prog < 50) return "fetching";
    if (prog < 70) return "processing";
    if (prog < 90) return "rendering";
    return "almostDone";
  }, []);
  
  // Rotate messages within the same phase
  React.useEffect(() => {
    if (!isLoading) return;
    
    const rotateMessage = () => {
      const messages = DYNAMIC_MESSAGES[currentPhaseKey];
      messageIndexRef.current = (messageIndexRef.current + 1) % messages.length;
      setStatusMessage(messages[messageIndexRef.current]);
    };
    
    const intervalId = setInterval(rotateMessage, messageRotationInterval);
    
    return () => clearInterval(intervalId);
  }, [isLoading, currentPhaseKey, messageRotationInterval]);
  
  // Initialize phases
  React.useEffect(() => {
    if (isLoading) {
      const phaseList = phases || LOADING_PHASES.dataPage;
      setCurrentPhases(
        phaseList.map((p, i) => ({
          ...p,
          status: i === 0 ? "in-progress" : "pending",
          startTime: i === 0 ? Date.now() : undefined,
        }))
      );
      setProgress(0);
      messageIndexRef.current = 0;
      setCurrentPhaseKey("initializing");
      setStatusMessage(getRandomMessage(DYNAMIC_MESSAGES.initializing));
    }
  }, [isLoading, phases]);
  
  // Animate progress
  React.useEffect(() => {
    if (!isLoading) {
      // Complete animation when loading finishes
      setProgress(100);
      setStatusMessage(getRandomMessage(DYNAMIC_MESSAGES.complete));
      setCurrentPhases((prev) =>
        prev.map((p) => ({ ...p, status: "completed" as const }))
      );
      return;
    }
    
    const startTime = Date.now();
    let animationFrame: number;
    let lastPhaseKey: keyof typeof DYNAMIC_MESSAGES = "initializing";
    
    const animate = () => {
      const elapsed = Date.now() - startTime;
      
      // Use easing function for smooth progress
      // Progress faster at start, slower near end (logarithmic easing)
      const targetProgress = Math.min(
        95, // Max 95% during loading, final 5% on complete
        (1 - Math.exp(-elapsed / (minDuration * 2))) * 100
      );
      
      setProgress(targetProgress);
      
      // Check if phase changed - if so, pick a new random message
      const newPhaseKey = getPhaseKey(targetProgress);
      if (newPhaseKey !== lastPhaseKey) {
        lastPhaseKey = newPhaseKey;
        setCurrentPhaseKey(newPhaseKey);
        messageIndexRef.current = Math.floor(Math.random() * DYNAMIC_MESSAGES[newPhaseKey].length);
        setStatusMessage(DYNAMIC_MESSAGES[newPhaseKey][messageIndexRef.current]);
      }
      
      // Update phases based on progress
      const phaseList = phases || LOADING_PHASES.dataPage;
      const phaseThresholds = phaseList.map((_, i) => ((i + 1) / phaseList.length) * 95);
      
      setCurrentPhases((prevPhases) => {
        if (prevPhases.length === 0) return prevPhases;
        
        return prevPhases.map((phase, index) => {
          const threshold = phaseThresholds[index];
          const prevThreshold = index > 0 ? phaseThresholds[index - 1] : 0;
          
          if (targetProgress >= threshold && phase.status !== "completed") {
            return { ...phase, status: "completed" as const, endTime: Date.now() };
          } else if (
            targetProgress >= prevThreshold &&
            targetProgress < threshold &&
            phase.status === "pending"
          ) {
            return { ...phase, status: "in-progress" as const, startTime: Date.now() };
          }
          return phase;
        });
      });
      
      if (targetProgress < 95) {
        animationFrame = requestAnimationFrame(animate);
      }
    };
    
    animationFrame = requestAnimationFrame(animate);
    
    return () => {
      if (animationFrame) {
        cancelAnimationFrame(animationFrame);
      }
    };
  }, [isLoading, minDuration, phases, getPhaseKey]);
  
  return {
    progress,
    statusMessage,
    phases: currentPhases,
    isLoading,
  };
}

import { useEffect, useState } from "react";

export function useCounterAnimation(
  endValue: number,
  duration: number = 2000,
  shouldAnimate: boolean = true
): number {
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!shouldAnimate) {
      setCount(endValue);
      return;
    }

    // Reset to 0 when endValue changes
    setCount(0);

    if (endValue === 0) return;

    const startTime = Date.now();
    const startValue = 0;

    const animate = () => {
      const now = Date.now();
      const progress = Math.min((now - startTime) / duration, 1);
      
      // Easing function for smooth animation
      const easeOutQuart = 1 - Math.pow(1 - progress, 4);
      
      const currentCount = Math.floor(startValue + (endValue - startValue) * easeOutQuart);
      setCount(currentCount);

      if (progress < 1) {
        requestAnimationFrame(animate);
      } else {
        setCount(endValue);
      }
    };

    const animationFrame = requestAnimationFrame(animate);

    return () => cancelAnimationFrame(animationFrame);
  }, [endValue, duration, shouldAnimate]);

  return count;
}

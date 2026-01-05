import { useEffect, useState, useRef } from "react";

export function useCounterAnimation(
  endValue: number,
  duration: number = 1900,
  shouldAnimate: boolean = true
): number {
  const [count, setCount] = useState(0);
  const prevValueRef = useRef(0);

  useEffect(() => {
    if (!shouldAnimate) {
      setCount(endValue);
      prevValueRef.current = endValue;
      return;
    }

    if (endValue === 0) {
      setCount(0);
      prevValueRef.current = 0;
      return;
    }

    const startTime = Date.now();
    const startValue = prevValueRef.current;
    const difference = endValue - startValue;

    // If the difference is small, use a shorter duration
    const adjustedDuration = Math.abs(difference) <= 5 ? duration * 0.5 : duration;

    const animate = () => {
      const now = Date.now();
      const progress = Math.min((now - startTime) / adjustedDuration, 1);
      
      // Easing function: easeOutCubic - faster ending
      const easeOutCubic = 1 - Math.pow(1 - progress, 3);
      
      const currentCount = Math.floor(startValue + difference * easeOutCubic);
      setCount(currentCount);

      if (progress < 1) {
        requestAnimationFrame(animate);
      } else {
        setCount(endValue);
        prevValueRef.current = endValue;
      }
    };

    const animationFrame = requestAnimationFrame(animate);

    return () => cancelAnimationFrame(animationFrame);
  }, [endValue, duration, shouldAnimate]);

  return count;
}

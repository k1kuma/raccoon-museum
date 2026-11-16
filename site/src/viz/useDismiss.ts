import { useEffect, type RefObject } from 'react';

/** Close a chart tooltip when the user taps/clicks outside the chart or presses Escape. */
export function useDismiss(ref: RefObject<HTMLElement | null>, open: boolean, close: () => void) {
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close();
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, close, ref]);
}

/** Place a tooltip beside an anchor x, flipping to the left in the right half so it never leaves the chart. */
export function sideStyle(anchorX: number, width: number, gap = 10) {
  const flip = anchorX > width / 2;
  return { left: anchorX + (flip ? -gap : gap), transform: flip ? 'translateX(-100%)' : undefined };
}

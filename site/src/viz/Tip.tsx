import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

interface Props {
  x: number; // anchor x within the chart box
  y: number; // anchor y within the chart box
  width: number; // chart box width, used for clamping
  place: 'above' | 'side';
  children: ReactNode;
}

/** Chart tooltip that measures itself and stays inside the chart box horizontally. */
export default function Tip({ x, y, width, place, children }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => { if (ref.current) setW(ref.current.offsetWidth); });
  let left: number;
  let top = y;
  let transform: string | undefined;
  if (place === 'above') {
    left = Math.min(Math.max(4, x - w / 2), Math.max(4, width - w - 4));
    transform = 'translateY(calc(-100% - 8px))';
  } else {
    const gap = 12;
    left = x + gap + w > width - 4 ? Math.max(4, x - gap - w) : x + gap;
  }
  return (
    <div ref={ref} className="viz-tt" style={{ left, top, transform, visibility: w ? 'visible' : 'hidden' }}>
      {children}
    </div>
  );
}

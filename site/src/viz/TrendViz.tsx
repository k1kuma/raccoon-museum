import { useCallback, useId, useMemo, useRef } from 'react';
import { ParentSize } from '@visx/responsive';
import { scaleLinear } from '@visx/scale';
import { LinePath, Line } from '@visx/shape';
import { Threshold } from '@visx/threshold';
import { GridRows } from '@visx/grid';
import { AxisBottom, AxisLeft } from '@visx/axis';
import { Group } from '@visx/group';
import { curveMonotoneX } from '@visx/curve';
import { localPoint } from '@visx/event';
import { useTooltip, Tooltip } from '@visx/tooltip';
import { useDismiss, sideStyle } from './useDismiss';

export interface Week { week: number; pts: number; med: number; avg: number; res: string; opp?: string }
interface Props { weeks: Week[]; label: string }

const INK = '#1d1e1f';
const GRAY = '#6b6f76';
const GREEN = '#137333';
const RED = '#cc0000';
const M = { top: 14, right: 16, bottom: 44, left: 42 };
const f2 = (n: number) => n.toFixed(2);
const diff = (n: number) => `${n >= 0 ? '+' : '−'}${Math.abs(n).toFixed(2)}`;

export default function TrendViz(props: Props) {
  return (
    <div className="viz viz-trend">
      <ParentSize initialSize={{ width: 600, height: 280 }} debounceTime={40}>
        {({ width, height }) => <Chart {...props} width={width || 600} height={height || 280} />}
      </ParentSize>
    </div>
  );
}

function Chart({ weeks, label, width, height }: Props & { width: number; height: number }) {
  const uid = useId().replace(/:/g, '');
  const box = useRef<HTMLDivElement>(null);
  const { tooltipOpen, tooltipData, tooltipLeft, showTooltip, hideTooltip } = useTooltip<number>();
  const iw = Math.max(40, width - M.left - M.right);
  const ih = Math.max(40, height - M.top - M.bottom);

  const { x, y } = useMemo(() => {
    const first = weeks[0].week;
    const last = weeks[weeks.length - 1].week;
    const vals = weeks.flatMap((w) => [w.pts, w.med, w.avg]);
    return {
      x: scaleLinear<number>({ domain: first === last ? [first - 1, last + 1] : [first, last], range: [0, iw] }),
      y: scaleLinear<number>({ domain: [Math.min(...vals) - 6, Math.max(...vals) + 6], range: [ih, 0], nice: true }),
    };
  }, [weeks, iw, ih]);

  const pick = useCallback((i: number) => {
    const j = Math.max(0, Math.min(weeks.length - 1, i));
    showTooltip({ tooltipData: j, tooltipLeft: M.left + x(weeks[j].week), tooltipTop: M.top });
  }, [weeks, x, showTooltip]);

  const fromEvent = (e: React.PointerEvent<SVGRectElement>) => {
    const p = localPoint(e);
    if (!p) return;
    const wk = x.invert(p.x - M.left);  // p is in SVG coords (viewBox = pixel size)
    let best = 0;
    weeks.forEach((w, i) => { if (Math.abs(w.week - wk) < Math.abs(weeks[best].week - wk)) best = i; });
    pick(best);
  };
  useDismiss(box, tooltipOpen, hideTooltip);

  const onKey = (e: React.KeyboardEvent) => {
    const cur = tooltipOpen && tooltipData != null ? tooltipData : weeks.length - 1;
    const moves: Record<string, number> = { ArrowRight: cur + 1, ArrowLeft: cur - 1, Home: 0, End: weeks.length - 1 };
    if (e.key in moves) { e.preventDefault(); pick(moves[e.key]); }
    else if (e.key === 'Escape') hideTooltip();
  };

  const sel = tooltipOpen && tooltipData != null ? weeks[tooltipData] : null;
  const every = iw / weeks.length < 26 ? 2 : 1;
  const tickWeeks = weeks.map((w) => w.week).filter((_, i) => i % every === 0);
  const ptsY = (w: Week) => y(w.pts);
  const say = sel
    ? `Week ${sel.week}${sel.opp ? ` vs ${sel.opp}` : ''}: ${f2(sel.pts)}, ${sel.res === 'W' ? 'won' : sel.res === 'L' ? 'lost' : 'tied'}. ` +
      `Median ${f2(sel.med)}, average ${f2(sel.avg)}.`
    : '';

  return (
    <div ref={box} className="viz-box">
      <svg
        width="100%" height="100%" viewBox={`0 0 ${width} ${height}`} role="group" tabIndex={0}
        aria-label={`${label} Use left and right arrow keys to step through weeks.`}
        onKeyDown={onKey} onFocus={(e) => (e.currentTarget as Element).matches(':focus-visible') && !tooltipOpen && pick(weeks.length - 1)} onBlur={hideTooltip}
      >
        <Group left={M.left} top={M.top}>
          <GridRows scale={y} width={iw} numTicks={5} stroke="#e6e7ea" />
          <Threshold<Week>
            id={`th-${uid}`} data={weeks} x={(w) => x(w.week)} y0={ptsY} y1={(w) => y(w.med)}
            clipAboveTo={0} clipBelowTo={ih} curve={curveMonotoneX}
            belowAreaProps={{ fill: RED, fillOpacity: 0.12 }} aboveAreaProps={{ fill: GREEN, fillOpacity: 0.14 }}
          />
          <LinePath data={weeks} x={(w) => x(w.week)} y={(w) => y(w.avg)} curve={curveMonotoneX}
            stroke={GRAY} strokeWidth={1.5} strokeDasharray="5 4" />
          <LinePath data={weeks} x={(w) => x(w.week)} y={(w) => y(w.med)} curve={curveMonotoneX}
            stroke={GRAY} strokeWidth={2} />
          <LinePath data={weeks} x={(w) => x(w.week)} y={ptsY} curve={curveMonotoneX}
            stroke={INK} strokeWidth={2.5} strokeLinejoin="round" />
          {sel && <Line from={{ x: x(sel.week), y: 0 }} to={{ x: x(sel.week), y: ih }} stroke={INK}
            strokeWidth={1} strokeDasharray="3 3" pointerEvents="none" />}
          {weeks.map((w, i) => (
            <circle key={w.week} cx={x(w.week)} cy={y(w.pts)} r={sel === w ? 7 : 4.5}
              fill={w.pts > w.med ? GREEN : w.pts < w.med ? RED : GRAY} stroke="#fff" strokeWidth={sel === w ? 2.5 : 1.5}
              pointerEvents="none" data-i={i} />
          ))}
          <AxisLeft scale={y} numTicks={5} hideAxisLine hideTicks tickClassName="viz-tick"
            tickLabelProps={{ fill: '#5c6066', fontSize: 11, textAnchor: 'end', dx: -4, dy: 4 }} />
          <AxisBottom scale={x} top={ih} tickValues={tickWeeks} tickFormat={(v) => `${v}`} stroke="#9aa0a6"
            tickStroke="#9aa0a6" tickClassName="viz-tick" label="Week" labelClassName="viz-axis-label"
            labelProps={{ fill: '#5c6066', fontSize: 11, textAnchor: 'middle' }} labelOffset={14}
            tickLabelProps={{ fill: '#5c6066', fontSize: 11, textAnchor: 'middle' }} />
          <rect x={-M.left} y={-M.top} width={width} height={height} fill="transparent" style={{ touchAction: 'pan-y', cursor: 'crosshair' }}
            onPointerMove={(e) => e.pointerType !== 'touch' && fromEvent(e)} onPointerDown={fromEvent}
            onPointerLeave={(e) => e.pointerType === 'mouse' && hideTooltip()} />
        </Group>
      </svg>
      {sel && tooltipLeft != null && (
        <Tooltip unstyled applyPositionStyle className="viz-tt" top={M.top} left={0}
          style={{ ...sideStyle(tooltipLeft, width) }}>
          <div className="viz-tt-h">
            <span>Week {sel.week}{sel.opp && <span className="viz-tt-sub"> vs {sel.opp}</span>}</span>
            <b className={`viz-res res-${sel.res}`}>{sel.res}</b>
          </div>
          <dl>
            <dt>Score</dt><dd><b>{f2(sel.pts)}</b></dd>
            <dt>Median</dt><dd>{f2(sel.med)} <span className={sel.pts >= sel.med ? 'pos' : 'neg'}>{diff(sel.pts - sel.med)}</span></dd>
            <dt>Average</dt><dd>{f2(sel.avg)} <span className={sel.pts >= sel.avg ? 'pos' : 'neg'}>{diff(sel.pts - sel.avg)}</span></dd>
          </dl>
        </Tooltip>
      )}
      <p className="sr-only" aria-live="polite">{say}</p>
    </div>
  );
}

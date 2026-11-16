import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { ParentSize } from '@visx/responsive';
import { scaleLinear } from '@visx/scale';
import { LinePath, Line } from '@visx/shape';
import { GridRows } from '@visx/grid';
import { AxisBottom, AxisLeft } from '@visx/axis';
import { Group } from '@visx/group';
import { localPoint } from '@visx/event';
import { useTooltip } from '@visx/tooltip';
import { useDismiss } from './useDismiss';
import Tip from './Tip';

type Metric = 'playoff' | 'bye' | 'title' | 'last';
export interface OddsWeek { week: number; teams: Record<string, Record<Metric, number>> }
export interface Team { id: string; name: string; color: string }
interface Props { history: OddsWeek[]; teams: Team[]; label: string }

const METRICS: [Metric, string][] = [['playoff', 'Playoffs'], ['bye', 'Bye'], ['title', 'Title'], ['last', 'Last place']];
const M = { top: 12, right: 14, bottom: 40, left: 40 };
const pct = (n: number) => `${Math.round(n * 100)}%`;

export default function OddsViz(props: Props) {
  const [metric, setMetric] = useState<Metric>('playoff');
  const [focus, setFocus] = useState<string | null>(null);
  useEffect(() => {
    try { const me = localStorage.getItem('ffm-me'); if (me && props.teams.some((t) => t.id === me)) setFocus(me); } catch {}
  }, []);
  return (
    <div className="viz viz-odds">
      <div className="viz-controls" role="group" aria-label="Which odds to chart">
        {METRICS.map(([k, l]) => (
          <button key={k} type="button" className="seg-btn" aria-pressed={metric === k} onClick={() => setMetric(k)}>{l}</button>
        ))}
      </div>
      <div className="viz-odds-plot">
        <ParentSize initialSize={{ width: 600, height: 300 }} debounceTime={40}>
          {({ width, height }) => <Chart {...props} metric={metric} focus={focus} width={width || 600} height={height || 300} />}
        </ParentSize>
      </div>
      <ul className="viz-legend" aria-label="Highlight a team">
        {props.teams.map((t) => (
          <li key={t.id}>
            <button type="button" aria-pressed={focus === t.id} onClick={() => setFocus(focus === t.id ? null : t.id)}>
              <i style={{ background: t.color }} />{t.name}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Chart({ history, teams, label, metric, focus, width, height }: Props & { metric: Metric; focus: string | null; width: number; height: number }) {
  const box = useRef<HTMLDivElement>(null);
  const { tooltipOpen, tooltipData, tooltipLeft, showTooltip, hideTooltip } = useTooltip<number>();
  useDismiss(box, tooltipOpen, hideTooltip);
  const iw = Math.max(40, width - M.left - M.right);
  const ih = Math.max(40, height - M.top - M.bottom);
  const first = history[0].week, last = history[history.length - 1].week;
  const x = useMemo(() => scaleLinear<number>({ domain: first === last ? [first - 1, last + 1] : [first, last], range: [0, iw] }), [first, last, iw]);
  const y = useMemo(() => scaleLinear<number>({ domain: [0, 1], range: [ih, 0] }), [ih]);
  const pick = (i: number) => {
    const j = Math.max(0, Math.min(history.length - 1, i));
    showTooltip({ tooltipData: j, tooltipLeft: M.left + x(history[j].week), tooltipTop: M.top });
  };
  const fromEvent = (e: React.PointerEvent<SVGRectElement>) => {
    const p = localPoint(e);
    if (!p) return;
    const wk = x.invert(p.x - M.left);
    let best = 0;
    history.forEach((h, i) => { if (Math.abs(h.week - wk) < Math.abs(history[best].week - wk)) best = i; });
    pick(best);
  };
  const onKey = (e: React.KeyboardEvent) => {
    const cur = tooltipOpen && tooltipData != null ? tooltipData : history.length - 1;
    const moves: Record<string, number> = { ArrowRight: cur + 1, ArrowLeft: cur - 1, Home: 0, End: history.length - 1 };
    if (e.key in moves) { e.preventDefault(); pick(moves[e.key]); } else if (e.key === 'Escape') hideTooltip();
  };
  const sel = tooltipOpen && tooltipData != null ? history[tooltipData] : null;
  const ordered = focus ? [...teams.filter((t) => t.id !== focus), ...teams.filter((t) => t.id === focus)] : teams;
  const every = iw / history.length < 26 ? 2 : 1;
  const metricName = METRICS.find(([k]) => k === metric)![1];
  const ranked = sel ? [...teams].sort((a, b) => (sel.teams[b.id]?.[metric] ?? 0) - (sel.teams[a.id]?.[metric] ?? 0)) : [];
  const say = sel ? `Week ${sel.week} ${metricName} odds: ${ranked.slice(0, 4).map((t) => `${t.name} ${pct(sel.teams[t.id]?.[metric] ?? 0)}`).join(', ')}` : '';

  return (
    <div ref={box} className="viz-box">
      <svg width="100%" height="100%" viewBox={`0 0 ${width} ${height}`} role="group" tabIndex={0}
        aria-label={`${label}: ${metricName} odds by week. Use left and right arrow keys to step through weeks.`}
        onKeyDown={onKey} onBlur={hideTooltip}
        onFocus={(e) => (e.currentTarget as Element).matches(':focus-visible') && !tooltipOpen && pick(history.length - 1)}>
        <Group left={M.left} top={M.top}>
          <GridRows scale={y} width={iw} tickValues={[0, 0.25, 0.5, 0.75, 1]} stroke="#e6e7ea" />
          {ordered.map((t) => {
            const dim = focus && focus !== t.id;
            return (
              <LinePath key={t.id} data={history} x={(h) => x(h.week)} y={(h) => y(h.teams[t.id]?.[metric] ?? 0)}
                stroke={dim ? '#c9ccd1' : t.color} strokeWidth={focus === t.id ? 3.5 : 2} strokeOpacity={dim ? 0.8 : 0.95}
                strokeLinejoin="round" />
            );
          })}
          {sel && <Line from={{ x: x(sel.week), y: 0 }} to={{ x: x(sel.week), y: ih }} stroke="#1d1e1f" strokeWidth={1}
            strokeDasharray="3 3" pointerEvents="none" />}
          <AxisLeft scale={y} tickValues={[0, 0.25, 0.5, 0.75, 1]} tickFormat={(v) => pct(Number(v))} hideAxisLine hideTicks
            tickLabelProps={{ fill: '#5c6066', fontSize: 11, textAnchor: 'end', dx: -4, dy: 4 }} />
          <AxisBottom scale={x} top={ih} tickValues={history.map((h) => h.week).filter((_, i) => i % every === 0)}
            tickFormat={(v) => `${v}`} stroke="#9aa0a6" tickStroke="#9aa0a6" label="After week" labelOffset={12}
            labelProps={{ fill: '#5c6066', fontSize: 11, textAnchor: 'middle' }}
            tickLabelProps={{ fill: '#5c6066', fontSize: 11, textAnchor: 'middle' }} />
          <rect x={-M.left} y={-M.top} width={width} height={height} fill="transparent" style={{ touchAction: 'pan-y', cursor: 'crosshair' }}
            onPointerMove={(e) => e.pointerType !== 'touch' && fromEvent(e)} onPointerDown={fromEvent}
            onPointerLeave={(e) => e.pointerType === 'mouse' && hideTooltip()} />
        </Group>
      </svg>
      {sel && tooltipLeft != null && (
        <Tip x={tooltipLeft} y={M.top} width={width} place="side">
          <div className="viz-tt-h"><span>After week {sel.week}</span><span className="viz-tt-sub">{metricName}</span></div>
          <dl>
            {ranked.slice(0, 6).map((t) => (
              <Fragment key={t.id}>
                <dt className={t.id === focus ? 'is-focus' : ''}><i className="viz-dot" style={{ background: t.color }} />{t.name}</dt>
                <dd>{pct(sel.teams[t.id]?.[metric] ?? 0)}</dd>
              </Fragment>
            ))}
          </dl>
        </Tip>
      )}
      <p className="sr-only" aria-live="polite">{say}</p>
    </div>
  );
}

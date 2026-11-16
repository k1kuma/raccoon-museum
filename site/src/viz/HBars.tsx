import { useRef } from 'react';
import { ParentSize } from '@visx/responsive';
import { scaleBand, scaleLinear } from '@visx/scale';
import { Bar } from '@visx/shape';
import { useTooltip } from '@visx/tooltip';
import { useDismiss } from './useDismiss';
import Tip from './Tip';

export interface Row { key: string; label: string; sub?: string; value: number; display: string; color?: string;
  tipTitle: string; tipLines: string[] }
interface Props { rows: Row[]; label: string; max?: number }

const ROW = 34;
const PAD = { top: 4, bottom: 4, right: 52 };

export default function HBars(props: Props) {
  const h = props.rows.length * ROW + PAD.top + PAD.bottom;
  return (
    <div className="viz viz-bars" style={{ height: h }}>
      <ParentSize initialSize={{ width: 600, height: h }} debounceTime={40}>
        {({ width }) => <Bars {...props} width={width || 600} height={h} />}
      </ParentSize>
    </div>
  );
}

function Bars({ rows, label, max, width, height }: Props & { width: number; height: number }) {
  const box = useRef<HTMLDivElement>(null);
  const { tooltipOpen, tooltipData, tooltipLeft, tooltipTop, showTooltip, hideTooltip } = useTooltip<number>();
  useDismiss(box, tooltipOpen, hideTooltip);
  const labelW = Math.min(170, Math.max(96, width * 0.36));
  const iw = Math.max(20, width - labelW - PAD.right);
  const x = scaleLinear<number>({ domain: [0, max ?? Math.max(...rows.map((r) => r.value), 0.0001)], range: [0, iw] });
  const y = scaleBand<number>({ domain: rows.map((_, i) => i), range: [PAD.top, height - PAD.bottom], padding: 0.28 });
  const chars = Math.floor((labelW - 10) / 6.6);
  const clip = (s: string) => (s.length > chars ? `${s.slice(0, chars - 1)}…` : s);
  const show = (i: number) => showTooltip({
    tooltipData: i, tooltipLeft: labelW + Math.max(2, x(Math.max(0, rows[i].value))) / 2, tooltipTop: y(i) ?? 0,
  });
  const sel = tooltipOpen && tooltipData != null ? rows[tooltipData] : null;

  return (
    <div ref={box} className="viz-box">
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} role="list" aria-label={label}>
        {rows.map((r, i) => {
          const by = y(i) ?? 0;
          const bw = Math.max(2, x(Math.max(0, r.value)));
          const active = tooltipData === i && tooltipOpen;
          return (
            <g key={r.key} role="listitem" tabIndex={0} className="viz-row"
              aria-label={`${r.tipTitle}: ${r.tipLines.join(', ')}`}
              onFocus={() => show(i)} onBlur={hideTooltip}
              onPointerEnter={(e) => e.pointerType === 'mouse' && show(i)}
              onPointerLeave={(e) => e.pointerType === 'mouse' && hideTooltip()}
              onPointerDown={() => show(i)}>
              <rect x={0} y={by - (ROW - y.bandwidth()) / 2} width={width} height={ROW}
                fill={active ? '#f1f2f4' : 'transparent'} />
              <text x={labelW - 8} y={by + y.bandwidth() / 2} dy="0.35em" textAnchor="end" className="viz-label">
                {clip(r.label)}{r.sub && <tspan className="viz-sub"> {r.sub}</tspan>}
              </text>
              <Bar x={labelW} y={by} width={bw} height={y.bandwidth()} rx={2} fill={r.color ?? '#3a3d42'} />
              <text x={labelW + bw + 6} y={by + y.bandwidth() / 2} dy="0.35em" className="viz-val">{r.display}</text>
            </g>
          );
        })}
      </svg>
      {sel && tooltipLeft != null && tooltipTop != null && (
        <Tip x={tooltipLeft} y={tooltipTop} width={width} place="above">
          <div className="viz-tt-h"><span>{sel.tipTitle}</span></div>
          <ul>{sel.tipLines.map((l) => <li key={l}>{l}</li>)}</ul>
        </Tip>
      )}
    </div>
  );
}

import fs from 'node:fs';
import path from 'node:path';
import satori from 'satori';
import { Resvg } from '@resvg/resvg-js';
import type { APIRoute, GetStaticPaths } from 'astro';
import { meta, managers, rivalries, records, shame, fun, season, champions, mgr, name, latestLabel } from '../../lib/data';

/** Build-time 1200×630 share cards (Open Graph / Twitter) in the site theme. */
const W = 1200;
const H = 630;
const root = process.cwd();
const font = (pkg: string, file: string) => fs.readFileSync(path.join(root, 'node_modules/@fontsource', pkg, 'files', file));
const fonts = [
  { name: 'Roboto', data: font('roboto', 'roboto-latin-400-normal.woff'), weight: 400 as const, style: 'normal' as const },
  { name: 'Roboto', data: font('roboto', 'roboto-latin-700-normal.woff'), weight: 700 as const, style: 'normal' as const },
  { name: 'Cond', data: font('roboto-condensed', 'roboto-condensed-latin-700-normal.woff'), weight: 700 as const, style: 'normal' as const },
];

const logoCache = new Map<string, string | null>();
function logoData(id: string | null | undefined): string | null {
  const rel = mgr(id).logo as string | undefined;
  if (!rel) return null;
  if (logoCache.has(rel)) return logoCache.get(rel)!;
  let out: string | null = null;
  try {
    const buf = fs.readFileSync(path.join(root, 'public', rel));
    const mime = buf[0] === 0x89 ? 'image/png' : buf[0] === 0xff ? 'image/jpeg' : null;
    out = mime ? `data:${mime};base64,${buf.toString('base64')}` : null;
  } catch { out = null; }
  logoCache.set(rel, out);
  return out;
}

type Node = { type: string; props: Record<string, any> };
const h = (type: string, style: Record<string, any>, ...children: any[]): Node => ({
  type,
  props: { style: { display: 'flex', ...style }, children: children.flat().filter((c) => c !== null && c !== undefined && c !== false) },
});
const clean = (s: string) => String(s ?? '').replace(/[\p{Extended_Pictographic}\u{FE0F}\u{200D}]/gu, '').replace(/\s+/g, ' ').trim();
const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);

function avatar(id: string | null | undefined, size: number): Node {
  const src = logoData(id);
  const m = mgr(id);
  if (src) return { type: 'img', props: { src, width: size, height: size, style: { borderRadius: size / 2, border: '4px solid #e3e4e7', objectFit: 'cover' } } };
  return h('div', { width: size, height: size, borderRadius: size / 2, background: m.color || '#555', color: '#fff', alignItems: 'center', justifyContent: 'center', fontFamily: 'Cond', fontSize: size * 0.42 }, m.initials || '?');
}

interface Card { kicker: string; title: string; sub?: string; people?: (string | null | undefined)[]; vs?: boolean; stat?: string }

function card(c: Card): Node {
  const people = [...new Set((c.people || []).filter(Boolean))].slice(0, 6) as string[];
  const big = people.length <= 2 ? 150 : people.length <= 3 ? 120 : 92;
  const title = clip(clean(c.title), 60);
  const side = people.length
    ? h('div', { flexDirection: people.length > 3 ? 'row' : 'column', flexWrap: 'wrap', width: people.length > 3 ? 320 : big + 20, gap: 14, alignItems: 'center', justifyContent: 'center' },
        c.vs && people.length === 2
          ? [avatar(people[0], big), h('div', { fontFamily: 'Cond', fontSize: 36, color: '#cc0000' }, 'VS'), avatar(people[1], big)]
          : people.map((p) => avatar(p, big)))
    : null;
  return h('div', { width: W, height: H, flexDirection: 'column', background: '#f1f2f4', fontFamily: 'Roboto' },
    h('div', { height: 92, background: '#1d1e1f', borderBottom: '8px solid #cc0000', alignItems: 'center', padding: '0 48px', gap: 18 },
      h('div', { width: 44, height: 44, background: '#cc0000', borderRadius: 4 }),
      h('div', { color: '#fff', fontFamily: 'Cond', fontSize: 34, letterSpacing: 1 }, clean(meta.leagueName).toUpperCase()),
      h('div', { color: '#b6b9be', fontSize: 22, marginLeft: 'auto' }, 'FANTASY FOOTBALL MUSEUM'),
    ),
    h('div', { flex: 1, margin: '32px 48px 0', background: '#fff', borderRadius: 6, padding: '32px 44px', gap: 36, alignItems: 'center' },
      h('div', { flexDirection: 'column', flex: 1, minWidth: 0 },
        h('div', { color: '#cc0000', fontFamily: 'Cond', fontSize: 28, letterSpacing: 2 }, clean(c.kicker).toUpperCase()),
        h('div', { borderLeft: '8px solid #cc0000', paddingLeft: 18, marginTop: 10, color: '#121213', fontFamily: 'Cond', fontSize: title.length > 28 ? 56 : 72, lineHeight: 1.05 }, title),
        c.stat ? h('div', { marginTop: 18, color: '#121213', fontFamily: 'Cond', fontSize: 52 }, clean(c.stat)) : null,
        c.sub ? h('div', { marginTop: 14, color: '#4a4e54', fontSize: 27, lineHeight: 1.3 }, clip(clean(c.sub), c.stat ? 115 : 165)) : null,
      ),
      side,
    ),
    h('div', { height: 64, alignItems: 'center', padding: '0 48px', color: '#5c6066', fontSize: 22 }, `Updated through ${latestLabel}`),
  );
}

const pct = (n: number) => `${Math.round(n * 100)}%`;
const rec = (w: number, l: number, t = 0) => `${w}-${l}${t ? `-${t}` : ''}`;

function cards(): Record<string, Card> {
  const out: Record<string, Card> = {};
  const champ = champions.banners[0];
  out.lobby = {
    kicker: 'The League Museum', title: meta.leagueName,
    sub: champ ? `Reigning champion: ${name(champ.podium[0]?.manager)} (${champ.season}). Records, rivalries, trophies and shame.` : 'Records, rivalries, trophies and shame.',
    people: meta.managers.filter((m: any) => m.active).map((m: any) => m.id),
  };
  const top = season.odds.rows.slice(0, 3);
  out.season = {
    kicker: `This Season · ${season.season}`, title: 'Playoff odds & power rankings',
    sub: `Best playoff odds: ${top.map((r: any) => `${name(r.manager)} ${pct(r.playoff)}`).join(' · ')}`, people: top.map((r: any) => r.manager),
  };
  for (const m of managers) {
    const c = m.career;
    out[`managers/${m.id}`] = {
      kicker: 'Manager Exhibit', title: m.name,
      stat: `${rec(c.wins, c.losses, c.ties)}${m.titles.length ? ` · ${m.titles.length}× champ` : ''}`,
      sub: m.caption, people: [m.id],
    };
  }
  for (const [pair, r] of Object.entries<any>(rivalries)) {
    out[`rivalry/${pair}`] = {
      kicker: 'Rivalry Room', title: `${name(r.a)} vs ${name(r.b)}`,
      stat: `${rec(r.aWins, r.bWins, r.ties)}`, sub: r.caption, people: [r.a, r.b], vs: true,
    };
  }
  for (const [section, list, label] of [['records', records, 'Record Book'], ['shame', shame, 'Hall of Shame']] as const) {
    for (const cat of list as any[]) {
      const e = cat.entries?.[0];
      out[`${section}/${cat.slug}`] = { kicker: label, title: cat.title, stat: e?.display, sub: e?.caption ?? cat.blurb, people: e ? [e.manager] : [] };
    }
  }
  for (const w of fun.weeks) {
    out[`fun/week/${w.id}`] = {
      kicker: 'Weekly awards', title: w.label,
      sub: w.awards.slice(0, 3).map((a: any) => `${a.title}: ${name(a.manager)}`).join(' · '),
      people: w.awards.map((a: any) => a.manager),
    };
  }
  for (const aw of fun.awards) {
    const lead = (fun.leaders[aw.slug] || [])[0];
    out[`fun/awards/${aw.slug}`] = {
      kicker: 'Fun & Trivia', title: aw.title,
      stat: lead ? `${name(lead.manager)} ×${lead.count}` : undefined, sub: aw.blurb, people: lead ? [lead.manager] : [],
    };
  }
  return out;
}

const ALL = cards();

export const getStaticPaths: GetStaticPaths = () => Object.keys(ALL).map((slug) => ({ params: { slug } }));

export const GET: APIRoute = async ({ params }) => {
  const svg = await satori(card(ALL[params.slug as string]) as any, { width: W, height: H, fonts });
  const png = new Resvg(svg, { fitTo: { mode: 'width', value: W } }).render().asPng();
  return new Response(png, { headers: { 'Content-Type': 'image/png' } });
};

// Pixel art for the office: one desk scene per agent, drawn as SVG rects so it
// stays crisp at any size. Poses and screens are a function of the agent's
// real status; nothing here animates on its own schedule to suggest work.

import type { ReactNode } from "react";

type Palette = Record<string, string>;

/** Turns rows of palette keys into rects, merging horizontal runs. */
function pixels(rows: string[], palette: Palette, ox = 0, oy = 0, keyPrefix = "p"): ReactNode[] {
  const out: ReactNode[] = [];
  rows.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      const key = row[x];
      const fill = palette[key];
      if (!fill) {
        x++;
        continue;
      }
      let end = x;
      while (end < row.length && row[end] === key) end++;
      out.push(<rect key={`${keyPrefix}-${x}-${y}`} x={ox + x} y={oy + y} width={end - x} height={1} fill={fill} />);
      x = end;
    }
  });
  return out;
}

// 16 × 20. Two spare rows on top for hats and buns.
const BODY = [
  "................",
  "................",
  "....hhhhhhhh....",
  "...hhhhhhhhhh...",
  "..hhhhhhhhhhhh..",
  "..hhsssssssshh..",
  "..hssssssssssh..",
  "..sswesssswess..",
  "..ssssssssssss..",
  "..sssssmmsssss..",
  "...ssssssssss...",
  ".....ssSSss.....",
  "...cccwccwccc...",
  "..cccccccccccc..",
  ".cccccccccccccc.",
  ".cccccccccccccc.",
  ".cCccccccccccCc.",
  ".cCccccccccccCc.",
  ".cCccccccccccCc.",
  ".cCccccccccccCc.",
];

// Overlays replace pixels of the base (a space keeps what is underneath).
const HAIR: Record<string, string[]> = {
  short: [],
  long: [
    "                ",
    "                ",
    "                ",
    "                ",
    "                ",
    " h            h ",
    " h            h ",
    " h            h ",
    " h            h ",
    " hh          hh ",
    " hh          hh ",
    "  hh        hh  ",
  ],
  bun: [
    "      hhhh      ",
    "     hhhhhh     ",
    "                ",
  ],
  curly: [
    "                ",
    "   hh hhhh hh   ",
    "  hhhhhhhhhhhh  ",
    " hhhhhhhhhhhhhh ",
    " hhhhhhhhhhhhhh ",
    " hh          hh ",
    " h            h ",
    " hh          hh ",
  ],
  spiky: [
    "   h  h  h  h   ",
    "  hhhhhhhhhhhh  ",
    "  hhhhhhhhhhhh  ",
  ],
  part: [
    "                ",
    "                ",
    "                ",
    "                ",
    "                ",
    "  hhhhhss       ",
    "  hhh           ",
  ],
  cap: [
    "                ",
    "    kkkkkkkk    ",
    "   kkkkkkkkkk   ",
    "  kkkkkkkkkkkk  ",
    " KKKKKKKKKKKKKKK",
    "  hh        hh  ",
  ],
  beret: [
    "       kk       ",
    "   kkkkkkkkkk   ",
    "  kkkkkkkkkkkkk ",
    "  kkkkkkkkkkkkkk",
    "  hhKKKKKKKKKK  ",
    "  hh        hh  ",
    "  h          h  ",
    "  h          h  ",
    "  h          h  ",
  ],
};

const EXTRAS: Record<string, string[]> = {
  glasses: [
    "                ",
    "                ",
    "                ",
    "                ",
    "                ",
    "                ",
    "                ",
    "  ggggggggggg   ",
    "   g  g g  g    ",
  ],
  headphones: [
    "                ",
    "   pppppppppp   ",
    "  pp        pp  ",
    "  p          p  ",
    "  p          p  ",
    "  p          p  ",
    " PP          PP ",
    " PP          PP ",
    " PP          PP ",
  ],
  headset: [
    "                ",
    "                ",
    "                ",
    "                ",
    "                ",
    "                ",
    " P              ",
    " P              ",
    " P              ",
    " pp             ",
    "  ppp           ",
  ],
};

function overlay(base: string[], top: string[]): string[] {
  return base.map((row, y) => {
    const over = top[y];
    if (!over) return row;
    let out = "";
    for (let x = 0; x < row.length; x++) out += over[x] && over[x] !== " " ? over[x] : row[x];
    return out;
  });
}

interface Look {
  hair: keyof typeof HAIR;
  hairColor: string;
  skin: string;
  extras?: (keyof typeof EXTRAS)[];
  hat?: string;
}

const LOOKS: Record<string, Look> = {
  scout: { hair: "cap", hairColor: "#5a3d28", skin: "#e2ab7c", hat: "#c9a45c" },
  writer: { hair: "long", hairColor: "#a8482b", skin: "#f3cfae", extras: ["glasses"] },
  clipper: { hair: "spiky", hairColor: "#181c2a", skin: "#c98958" },
  editor: { hair: "short", hairColor: "#4a3626", skin: "#f0c39a", extras: ["headphones"] },
  designer: { hair: "beret", hairColor: "#2b2f4d", skin: "#8f5d3e", hat: "#e2557a" },
  publisher: { hair: "bun", hairColor: "#e3bf5b", skin: "#f6d9bd" },
  community: { hair: "curly", hairColor: "#3a2419", skin: "#b0774f", extras: ["headset"] },
  analyst: { hair: "part", hairColor: "#8d93a3", skin: "#e6b58a", extras: ["glasses"] },
};

function shade(hex: string, amount: number): string {
  const value = hex.replace("#", "");
  const channel = (index: number) => {
    const raw = parseInt(value.slice(index, index + 2), 16);
    const next = amount < 0 ? raw * (1 + amount) : raw + (255 - raw) * amount;
    return Math.max(0, Math.min(255, Math.round(next))).toString(16).padStart(2, "0");
  };
  return `#${channel(0)}${channel(2)}${channel(4)}`;
}

export type DeskStatus = "working" | "error" | "awaiting_approval" | "blocked" | "queued" | "idle";

const SCREEN: Record<DeskStatus, { bg: string; fg: string }> = {
  working: { bg: "#0f2a2e", fg: "#5eead4" },
  awaiting_approval: { bg: "#3a2c08", fg: "#fbbf24" },
  blocked: { bg: "#32201a", fg: "#f59e0b" },
  error: { bg: "#3a1220", fg: "#fb7185" },
  queued: { bg: "#17233f", fg: "#7aa2f7" },
  idle: { bg: "#151826", fg: "#3b4160" },
};

// 13 × 9 glyphs shown on the monitor.
const SCREEN_GLYPH: Record<DeskStatus, string[]> = {
  working: [],
  awaiting_approval: ["......#......", "......#......", "......#......", "......#......", "......#......", ".............", "......#......"],
  blocked: ["....#####....", "....#...#....", "....#...#....", "...#######...", "...###.###...", "...###.###...", "...#######..."],
  error: ["...#.....#...", "....#...#....", ".....#.#.....", "......#......", ".....#.#.....", "....#...#....", "...#.....#..."],
  queued: ["....#####....", "...#..#..#...", "...#..#..#...", "...#..###.#..", "...#.....#...", "...#.....#...", "....#####...."],
  idle: [".............", ".............", ".............", "...#..#..#...", ".............", ".............", "............."],
};

/** Small prop on the right of each desk that tells the agents apart. */
function accessory(agent: string, color: string): ReactNode {
  const dark = "#0e0f1a";
  switch (agent) {
    case "scout": // magnifying glass over a map
      return (
        <g>
          <rect x={44} y={31} width={14} height={3} fill="#e9dcc0" />
          <rect x={46} y={32} width={4} height={1} fill="#c9a45c" />
          <rect x={52} y={32} width={3} height={1} fill="#7aa2f7" />
          <rect x={47} y={22} width={6} height={1} fill={dark} />
          <rect x={46} y={23} width={1} height={5} fill={dark} />
          <rect x={53} y={23} width={1} height={5} fill={dark} />
          <rect x={47} y={28} width={6} height={1} fill={dark} />
          <rect x={47} y={23} width={6} height={5} fill="#bfe9f5" opacity={0.85} />
          <rect x={48} y={24} width={2} height={1} fill="#ffffff" />
          <rect x={53} y={28} width={2} height={2} fill={dark} />
          <rect x={55} y={30} width={2} height={2} fill="#8a5a33" />
        </g>
      );
    case "writer": // notebook, pencil and a mug
      return (
        <g>
          <rect x={44} y={29} width={9} height={5} fill="#f4f1e8" />
          <rect x={44} y={29} width={1} height={5} fill={color} />
          <rect x={46} y={30} width={5} height={1} fill="#9aa0b5" />
          <rect x={46} y={32} width={4} height={1} fill="#9aa0b5" />
          <rect x={54} y={27} width={5} height={7} fill="#f4f1e8" />
          <rect x={54} y={27} width={5} height={1} fill="#c8c4b6" />
          <rect x={59} y={29} width={1} height={3} fill="#f4f1e8" />
          <rect x={55} y={24} width={1} height={2} fill="#c8ccda" opacity={0.7} />
          <rect x={57} y={23} width={1} height={3} fill="#c8ccda" opacity={0.7} />
        </g>
      );
    case "clipper": // film strip and scissors
      return (
        <g>
          <rect x={44} y={28} width={15} height={6} fill="#1a1d2c" />
          {[45, 48, 51, 54, 57].map((x) => (
            <rect key={x} x={x} y={28} width={1} height={1} fill="#e8eaf6" />
          ))}
          {[45, 48, 51, 54, 57].map((x) => (
            <rect key={`b${x}`} x={x} y={33} width={1} height={1} fill="#e8eaf6" />
          ))}
          <rect x={45} y={30} width={3} height={2} fill={color} />
          <rect x={50} y={30} width={3} height={2} fill="#fbbf24" />
          <rect x={55} y={30} width={3} height={2} fill="#fb7185" />
          <rect x={49} y={22} width={1} height={5} fill="#c8ccda" />
          <rect x={53} y={22} width={1} height={5} fill="#c8ccda" />
          <rect x={50} y={24} width={3} height={1} fill="#c8ccda" />
        </g>
      );
    case "editor": // clapperboard
      return (
        <g>
          <rect x={45} y={25} width={13} height={9} fill="#1a1d2c" />
          <rect x={45} y={25} width={13} height={3} fill="#e8eaf6" />
          {[46, 50, 54].map((x) => (
            <rect key={x} x={x} y={25} width={2} height={3} fill="#1a1d2c" />
          ))}
          <rect x={47} y={30} width={9} height={1} fill={color} />
          <rect x={47} y={32} width={6} height={1} fill="#9aa0b5" />
        </g>
      );
    case "designer": // palette with paint
      return (
        <g>
          <rect x={45} y={27} width={13} height={7} fill="#d9b382" />
          <rect x={44} y={29} width={1} height={4} fill="#d9b382" />
          <rect x={58} y={29} width={1} height={3} fill="#d9b382" />
          <rect x={47} y={28} width={2} height={2} fill="#fb7185" />
          <rect x={50} y={28} width={2} height={2} fill="#fbbf24" />
          <rect x={53} y={28} width={2} height={2} fill="#5eead4" />
          <rect x={55} y={31} width={2} height={2} fill="#7aa2f7" />
          <rect x={47} y={31} width={3} height={2} fill="#1b1d2e" />
          <rect x={52} y={21} width={1} height={6} fill="#8a5a33" />
          <rect x={52} y={19} width={1} height={2} fill={color} />
        </g>
      );
    case "publisher": // megaphone
      return (
        <g>
          <rect x={45} y={27} width={4} height={4} fill={color} />
          <rect x={49} y={26} width={2} height={6} fill="#e8eaf6" />
          <rect x={51} y={25} width={2} height={8} fill="#e8eaf6" />
          <rect x={53} y={24} width={2} height={10} fill="#e8eaf6" />
          <rect x={55} y={24} width={1} height={10} fill="#c8ccda" />
          <rect x={46} y={31} width={2} height={3} fill="#9aa0b5" />
          <rect x={57} y={26} width={1} height={1} fill="#fbbf24" />
          <rect x={58} y={28} width={2} height={1} fill="#fbbf24" />
          <rect x={57} y={31} width={1} height={1} fill="#fbbf24" />
        </g>
      );
    case "community": // speech bubbles and a heart
      return (
        <g>
          <rect x={44} y={22} width={10} height={6} fill="#e8eaf6" />
          <rect x={46} y={28} width={2} height={1} fill="#e8eaf6" />
          <rect x={46} y={24} width={6} height={1} fill="#6b7190" />
          <rect x={46} y={26} width={4} height={1} fill="#6b7190" />
          <rect x={51} y={29} width={9} height={5} fill={color} />
          <rect x={57} y={34} width={2} height={1} fill={color} />
          <rect x={53} y={30} width={1} height={1} fill="#1b1d2e" />
          <rect x={55} y={30} width={1} height={1} fill="#1b1d2e" />
          <rect x={53} y={31} width={3} height={1} fill="#1b1d2e" />
          <rect x={54} y={32} width={1} height={1} fill="#1b1d2e" />
        </g>
      );
    case "analyst": // bar chart
      return (
        <g>
          <rect x={45} y={22} width={14} height={12} fill="#f4f1e8" />
          <rect x={45} y={22} width={14} height={1} fill="#c8c4b6" />
          <rect x={47} y={29} width={2} height={3} fill="#7aa2f7" />
          <rect x={50} y={27} width={2} height={5} fill="#fbbf24" />
          <rect x={53} y={25} width={2} height={7} fill={color} />
          <rect x={56} y={28} width={1} height={4} fill="#fb7185" />
          <rect x={46} y={32} width={12} height={1} fill="#6b7190" />
        </g>
      );
    default:
      return null;
  }
}

export function DeskScene({ agent, color, status, title }: { agent: string; color: string; status: DeskStatus; title: string }) {
  const look = LOOKS[agent] ?? LOOKS.scout;
  let rows = BODY;
  rows = overlay(rows, HAIR[look.hair] ?? []);
  for (const extra of look.extras ?? []) rows = overlay(rows, EXTRAS[extra]);
  const palette: Palette = {
    h: look.hairColor,
    s: look.skin,
    S: shade(look.skin, -0.18),
    e: "#1b1d2e",
    w: "#ffffff",
    m: shade(look.skin, -0.38),
    c: color,
    C: shade(color, -0.25),
    k: look.hat ?? color,
    K: shade(look.hat ?? color, -0.25),
    g: "#1b1d2e",
    p: "#2b2f4d",
    P: "#fb7185",
  };
  const screen = SCREEN[status];
  const sleeve = shade(color, -0.12);
  const raised = status === "awaiting_approval";
  return (
    <svg className={`desk-svg desk-${status}`} viewBox="0 0 64 56" shapeRendering="crispEdges" role="img" aria-label={title}>
      {/* chair */}
      <rect x={21} y={16} width={22} height={22} fill="#2b2f4d" />
      <rect x={22} y={17} width={20} height={20} fill="#3a3f63" />
      <rect x={30} y={38} width={4} height={12} fill="#1a1d2c" />
      {/* character */}
      <g className="sprite-body">{pixels(rows, palette, 24, 6, "b")}</g>
      <g className="sprite-blink">
        <rect x={27} y={13} width={2} height={1} fill={look.skin} />
        <rect x={34} y={13} width={2} height={1} fill={look.skin} />
      </g>
      {/* monitor */}
      <rect x={5} y={15} width={17} height={14} fill="#0e0f1a" />
      <rect x={7} y={17} width={13} height={10} fill={screen.bg} />
      {status === "working" ? (
        <g className="screen-lines">
          <rect x={8} y={18} width={8} height={1} fill={screen.fg} />
          <rect x={8} y={20} width={11} height={1} fill={screen.fg} opacity={0.7} />
          <rect x={8} y={22} width={6} height={1} fill={screen.fg} />
          <rect x={8} y={24} width={9} height={1} fill={screen.fg} opacity={0.7} />
          <rect className="screen-caret" x={18} y={24} width={1} height={1} fill="#ffffff" />
        </g>
      ) : (
        <g className={status === "awaiting_approval" || status === "error" ? "screen-flash" : ""}>{pixels(SCREEN_GLYPH[status], { "#": screen.fg }, 7, 18, "g")}</g>
      )}
      <rect x={12} y={29} width={3} height={4} fill="#0e0f1a" />
      <rect x={9} y={33} width={9} height={1} fill="#0e0f1a" />
      {/* desk */}
      <rect x={3} y={34} width={58} height={1} fill="#c98f5b" />
      <rect x={3} y={35} width={58} height={3} fill="#a8703f" />
      <rect x={5} y={38} width={54} height={12} fill="#7c4f2a" />
      <rect x={5} y={38} width={54} height={1} fill="#5f3b1e" />
      <rect x={8} y={41} width={20} height={6} fill="#6b4323" />
      <rect x={17} y={43} width={2} height={1} fill="#c98f5b" />
      <rect x={5} y={50} width={3} height={4} fill="#5f3b1e" />
      <rect x={56} y={50} width={3} height={4} fill="#5f3b1e" />
      {/* keyboard */}
      <rect x={26} y={32} width={12} height={2} fill="#c8ccda" />
      <rect x={27} y={32} width={10} height={1} fill="#e8eaf6" />
      {/* arms */}
      {raised ? (
        <g className="arm-raised">
          <rect x={39} y={12} width={3} height={10} fill={sleeve} />
          <rect x={39} y={9} width={3} height={3} fill={look.skin} />
          <rect x={37} y={2} width={7} height={8} fill="#ffffff" />
          <rect x={38} y={4} width={5} height={1} fill="#9aa0b5" />
          <rect x={38} y={6} width={4} height={1} fill="#9aa0b5" />
          <rect x={27} y={30} width={3} height={2} fill={look.skin} />
        </g>
      ) : (
        <g>
          <rect className="hand hand-left" x={27} y={30} width={3} height={2} fill={look.skin} />
          <rect className="hand hand-right" x={34} y={30} width={3} height={2} fill={look.skin} />
        </g>
      )}
      {status === "blocked" ? (
        <g>
          <rect x={28} y={29} width={8} height={5} fill="#fbbf24" />
          <rect x={30} y={27} width={4} height={2} fill="#fbbf24" />
          <rect x={31} y={25} width={2} height={2} fill="#fbbf24" />
          <rect x={31} y={28} width={2} height={3} fill="#1b1d2e" />
          <rect x={31} y={32} width={2} height={1} fill="#1b1d2e" />
        </g>
      ) : null}
      {status === "error" ? (
        <g className="sweat">
          <rect x={41} y={10} width={1} height={2} fill="#7aa2f7" />
          <rect x={40} y={12} width={3} height={2} fill="#7aa2f7" />
        </g>
      ) : null}
      {status === "idle" ? (
        <g>
          <rect x={39} y={29} width={4} height={5} fill="#f4f1e8" />
          <rect x={43} y={30} width={1} height={3} fill="#f4f1e8" />
          <rect x={39} y={29} width={4} height={1} fill="#6b4323" />
        </g>
      ) : null}
      {accessory(agent, color)}
    </svg>
  );
}

export function Plant({ className = "" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 24" shapeRendering="crispEdges" aria-hidden="true">
      <rect x={7} y={2} width={2} height={12} fill="#2f8f5b" />
      <rect x={3} y={4} width={4} height={2} fill="#3cb371" />
      <rect x={2} y={6} width={3} height={2} fill="#2f8f5b" />
      <rect x={9} y={3} width={4} height={2} fill="#3cb371" />
      <rect x={11} y={5} width={3} height={2} fill="#2f8f5b" />
      <rect x={4} y={9} width={3} height={2} fill="#3cb371" />
      <rect x={9} y={8} width={3} height={2} fill="#3cb371" />
      <rect x={6} y={0} width={4} height={3} fill="#3cb371" />
      <rect x={4} y={14} width={8} height={2} fill="#c9793f" />
      <rect x={5} y={16} width={6} height={6} fill="#a85f2c" />
      <rect x={5} y={16} width={6} height={1} fill="#8a4a1f" />
    </svg>
  );
}

export function WaterCooler({ className = "" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 14 30" shapeRendering="crispEdges" aria-hidden="true">
      <rect x={3} y={0} width={8} height={10} fill="#8ecbf0" />
      <rect x={4} y={1} width={2} height={6} fill="#c9e9fb" />
      <rect x={2} y={10} width={10} height={2} fill="#c8ccda" />
      <rect x={2} y={12} width={10} height={16} fill="#e8eaf6" />
      <rect x={2} y={12} width={2} height={16} fill="#c8ccda" />
      <rect x={6} y={15} width={2} height={2} fill="#fb7185" />
      <rect x={9} y={15} width={2} height={2} fill="#7aa2f7" />
      <rect x={3} y={28} width={8} height={2} fill="#6b7190" />
    </svg>
  );
}

/** A sheet of paper that travels between desks when a real handoff is recorded. */
export function PaperSprite() {
  return (
    <svg viewBox="0 0 10 12" shapeRendering="crispEdges" width={30} height={36} aria-hidden="true">
      <rect x={0} y={0} width={10} height={12} fill="#0e0f1a" />
      <rect x={1} y={1} width={8} height={10} fill="#ffffff" />
      <rect x={2} y={3} width={6} height={1} fill="#6b7190" />
      <rect x={2} y={5} width={6} height={1} fill="#6b7190" />
      <rect x={2} y={7} width={4} height={1} fill="#6b7190" />
    </svg>
  );
}

export const AGENT_KEYS = Object.keys(LOOKS);

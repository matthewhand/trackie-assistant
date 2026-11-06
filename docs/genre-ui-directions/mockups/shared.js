// Synthetic data + helpers shared by the three direction mockups.
// Every title, style and cover below is invented / generated; nothing comes from a real library.
const GENRES = [
  { name: "Neo Soul & House", n: 30, hue: 255 },
  { name: "Lo-fi Study Beats", n: 18, hue: 170 },
  { name: "Synthwave", n: 18, hue: 330, kids: [["Night Drive", 10], ["Neon Rain", 8]] },
  { name: "Cinematic Ambient", n: 14, hue: 42 },
  { name: "Folk Acoustic", n: 11, hue: 130 },
  { name: "Cyber Industrial", n: 6, hue: 5 },
  { name: "Uncategorised", n: 9, hue: 215 },
];
const ADJ = ["Velvet", "Neon", "Paper", "Quiet", "Golden", "Static", "Hollow", "Amber", "Glass", "Slow"];
const NOUN = ["Garden", "Signal", "Orbit", "Harbor", "Lantern", "Engine", "Meadow", "Circuit", "Tide", "Window"];
const STYLES = {
  "Neo Soul & House": ["neo soul, warm keys, 4/4 groove", "deep house, soft vocals, rhodes", "soulful house, brushed hats"],
  "Lo-fi Study Beats": ["lo-fi hip hop, dusty drums, mellow", "chillhop, vinyl crackle, soft piano"],
  "Synthwave": ["synthwave, gated drums, night drive", "retrowave, arpeggios, neon rain"],
  "Cinematic Ambient": ["ambient pads, slow swells, strings", "drone, warm textures, field recording"],
  "Folk Acoustic": ["acoustic guitar, fingerpicking, warm vocals", "folk, banjo, hand claps"],
  "Cyber Industrial": ["industrial, distorted bass, machine rhythm"],
  "Uncategorised": ["unclear mix of ambient and downtempo", "genre-blend experiment", "spoken word over pads"],
};
function rng(seed) { let s = seed * 9301 + 49297; return () => ((s = (s * 9301 + 49297) % 233280) / 233280); }
function art(seed, hue) {
  const r = rng(seed + 3), h2 = (hue + 40 + Math.floor(r() * 60)) % 360;
  let shapes = "";
  for (let i = 0; i < 4; i++)
    shapes += `<circle cx='${Math.floor(r() * 100)}' cy='${Math.floor(r() * 100)}' r='${10 + Math.floor(r() * 30)}' fill='hsl(${(hue + i * 25) % 360} 70% ${55 + i * 5}%)' opacity='.35'/>`;
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='hsl(${hue} 60% 28%)'/><stop offset='1' stop-color='hsl(${h2} 65% 45%)'/></linearGradient></defs><rect width='100' height='100' fill='url(#g)'/>${shapes}</svg>`;
  const enc = encodeURIComponent(svg).replace(/'/g, "%27").replace(/\(/g, "%28").replace(/\)/g, "%29");
  return "url(data:image/svg+xml;utf8," + enc + ")";
}
const TRACKS = [];
{
  let id = 0;
  for (const g of GENRES) {
    for (let i = 0; i < g.n; i++, id++) {
      const r = rng(id + 11);
      const kid = g.kids ? (i < g.kids[0][1] ? g.kids[0][0] : g.kids[1][0]) : null;
      TRACKS.push({
        id, genre: g.name, kid, hue: g.hue,
        title: `${ADJ[(id * 3) % ADJ.length]} ${NOUN[(id * 7 + 2) % NOUN.length]} ${String.fromCharCode(65 + (id % 7))}`,
        style: STYLES[g.name][i % STYLES[g.name].length],
        dur: `${2 + Math.floor(r() * 3)}:${String(Math.floor(r() * 60)).padStart(2, "0")}`,
        conf: g.name === "Uncategorised" ? 0.22 + r() * 0.3 : 0.55 + r() * 0.44,
      });
    }
  }
}
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const P = new URLSearchParams(location.search);
document.documentElement.dataset.theme = P.get("theme") === "light" ? "light" : "dark";
const gcolor = (name, l = 62) => `hsl(${(GENRES.find((g) => g.name === name) || { hue: 215 }).hue} 70% ${l}%)`;

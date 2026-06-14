const cp = {
  base:'#1e1e2e', mantle:'#181825', crust:'#11111b',
  surface0:'#313244', surface1:'#45475a', surface2:'#585b70',
  text:'#cdd6f4', subtext0:'#a6adc8', subtext1:'#bac2de',
  overlay0:'#6c7086', overlay1:'#7f849c', overlay2:'#9399b2',
  rosewater:'#f5e0dc', flamingo:'#f2cdcd', pink:'#f5c2e7',
  mauve:'#cba6f7', red:'#f38ba8', maroon:'#eba0ac',
  peach:'#fab387', yellow:'#f9e2af', green:'#a6e3a1',
  teal:'#94e2d5', sky:'#89dceb', sapphire:'#74c7ec',
  blue:'#89b4fa', lavender:'#b4befe',
};
const names = Object.keys(cp);
module.exports = {
  content: ['static/index.html'],
  // dynamic classes built in JS template strings (text-${color}, bg-${color})
  // PLUS layout utilities that drive the 3-column grid — safelisted so a future
  // grid tweak can't silently drop them (task-4114 footgun: stale CSS → broken layout).
  safelist: [
    ...names.map(n => `text-${n}`),
    ...names.map(n => `bg-${n}`),
    ...names.map(n => `border-${n}`),
    'grid', 'grid-cols-12', 'hidden', 'block',
    ...Array.from({ length: 12 }, (_, i) => `col-span-${i + 1}`),
    ...Array.from({ length: 12 }, (_, i) => `lg:col-span-${i + 1}`),
    'lg:block', 'lg:hidden',
  ],
  theme: { extend: {
    fontFamily: { pixel:['"Press Start 2P"','monospace'], mono:['"JetBrains Mono"','monospace'] },
    colors: cp,
    animation: { 'fade-in':'fadeIn 0.2s ease-out', 'pulse-glow':'pulseGlow 0.8s ease-out' },
  }},
};

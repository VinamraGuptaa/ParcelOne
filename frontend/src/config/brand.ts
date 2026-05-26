export const BRAND_NAME = 'icy-disk';
export const BRAND_TAGLINE = 'Land records, IGR, and litigation intelligence';

export const BRAND_COLORS = {
  newsprint: '#f0ece2',
  paper:     '#f7f4ed',
  ink:       '#111009',
  mid:       '#ede8de',
  rule:      '#c8c0b0',
  ghost:     '#a89878',
  red:       '#c8302a',
  amber:     '#c8780a',
  green:     '#1a5a1a',
  brass:     '#c8a96a',
} as const;

export const BRAND_FONTS = {
  display: "'DM Serif Display', serif",
  prose:   "'EB Garamond', serif",
  mono:    "'DM Mono', monospace",
} as const;

/** Three-bar logo mark: [width, height] pairs, gap=2px, color=red */
export const BRAND_LOGO_MARK = [
  { width: 28, height: 4 },
  { width: 20, height: 4 },
  { width: 13, height: 4 },
] as const;

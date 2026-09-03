// Shared Tailwind Play-CDN config for the dashboard's 3 screens. Loaded
// after cdn.tailwindcss.com, before any utility classes are used, so
// `bg-paper`, `text-ink`, `font-sans` etc. resolve consistently on every
// page without triplicating the same config block (docs FR-12: "no
// build step" -- this is the DRY mechanism available inside that
// constraint, same reasoning as shared.js for the JS helpers).
//
// Palette concept: a ledger, not a dashboard skin -- warm paper instead
// of stark white, ink instead of default slate-900, a single deep-indigo
// "signature" accent for brand/nav/links that stays out of the way of
// the state colors (emerald/rose/amber/orange), which carry real meaning
// (recovered/abandoned/scheduled/escalating) and are never used as decor.
tailwind.config = {
  theme: {
    extend: {
      colors: {
        paper: "#f6f3ec",
        surface: "#ffffff",
        ink: "#181510",
        line: "#e4ddc9",
        muted: "#7d7563",
        accent: {
          DEFAULT: "#2f3d76",
          soft: "#eceff7",
          deep: "#232d59",
        },
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
};

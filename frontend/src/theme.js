export const PALETTES = {
  latte: [
    ["rosewater", "Rosewater", "#dc8a78"],
    ["flamingo", "Flamingo", "#dd7878"],
    ["pink", "Pink", "#ea76cb"],
    ["mauve", "Mauve", "#8839ef"],
    ["red", "Red", "#d20f39"],
    ["maroon", "Maroon", "#e64553"],
    ["peach", "Peach", "#fe640b"],
    ["yellow", "Yellow", "#df8e1d"],
    ["green", "Green", "#40a02b"],
    ["teal", "Teal", "#179299"],
    ["sky", "Sky", "#04a5e5"],
    ["sapphire", "Sapphire", "#209fb5"],
    ["blue", "Blue", "#1e66f5"],
    ["lavender", "Lavender", "#7287fd"],
  ],
  mocha: [
    ["rosewater", "Rosewater", "#f5e0dc"],
    ["flamingo", "Flamingo", "#f2cdcd"],
    ["pink", "Pink", "#f5c2e7"],
    ["mauve", "Mauve", "#cba6f7"],
    ["red", "Red", "#f38ba8"],
    ["maroon", "Maroon", "#eba0ac"],
    ["peach", "Peach", "#fab387"],
    ["yellow", "Yellow", "#f9e2af"],
    ["green", "Green", "#a6e3a1"],
    ["teal", "Teal", "#94e2d5"],
    ["sky", "Sky", "#89dceb"],
    ["sapphire", "Sapphire", "#74c7ec"],
    ["blue", "Blue", "#89b4fa"],
    ["lavender", "Lavender", "#b4befe"],
  ],
};

export function normalizeTheme(value) {
  const aliases = { dark: "mocha", light: "latte" };
  const normalized = aliases[value] || value;
  return ["system", "latte", "mocha"].includes(normalized) ? normalized : "system";
}

export function effectiveFlavor(theme, systemDark) {
  return theme === "mocha" || (theme === "system" && systemDark) ? "mocha" : "latte";
}

export function paletteFor(flavor, key) {
  return PALETTES[flavor].find(([name]) => name === key) || PALETTES[flavor][3];
}

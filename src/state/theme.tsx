import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { loadPref, savePref } from "../lib/storage";

export type Theme = "dark" | "light";
const KEY = "ol-theme";

const ThemeCtx = createContext<{ theme: Theme; toggle: () => void }>({ theme: "dark", toggle: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => (loadPref(KEY) === "light" ? "light" : "dark"));
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    savePref(KEY, theme);
  }, [theme]);
  // The attribute changes before the re-render, so charts re-read the new theme's CSS variables (usePalette).
  const toggle = useCallback(() => setTheme((t) => {
    const next = t === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    return next;
  }), []);
  return <ThemeCtx.Provider value={{ theme, toggle }}>{children}</ThemeCtx.Provider>;
}

export const useTheme = () => useContext(ThemeCtx);

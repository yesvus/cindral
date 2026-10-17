"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/admin";
import { Moon, Sun } from "lucide-react";

const THEME_STORAGE_KEY = "cindral-theme";

export function ThemeToggle() {
  // the bootstrap script in the root layout has already resolved the theme by
  // the time this hydrates, so the first client render reads light and the
  // effect corrects it without a hydration mismatch
  const [theme, setTheme] = useState<"light" | "dark">("light");

  useEffect(() => {
    setTheme(document.documentElement.dataset.adminTheme === "dark" ? "dark" : "light");
  }, []);

  function toggle() {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.adminTheme = next;
    window.localStorage.setItem(THEME_STORAGE_KEY, next);
    setTheme(next);
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      onClick={toggle}
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
    >
      {theme === "dark" ? <Sun /> : <Moon />}
    </Button>
  );
}

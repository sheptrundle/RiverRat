// Favorite reaches, kept in this browser (localStorage) and shared by every page.

import { useEffect, useState } from "react";

const KEY = "riverrat.favorites";
const CHANGED = "riverrat:favorites";

function load(): Set<number> {
  try {
    return new Set(JSON.parse(localStorage.getItem(KEY) ?? "[]"));
  } catch {
    return new Set();
  }
}

function save(ids: Set<number>) {
  try {
    localStorage.setItem(KEY, JSON.stringify([...ids]));
  } catch {
    // private window or blocked storage: favorites last until the page closes
  }
  window.dispatchEvent(new Event(CHANGED));
}

let current = load();

export function useFavorites(): [Set<number>, (id: number) => void] {
  const [favorites, setFavorites] = useState(current);
  useEffect(() => {
    const update = () => setFavorites(current);
    // another tab changed them
    const fromStorage = (e: StorageEvent) => {
      if (e.key === KEY) {
        current = load();
        update();
      }
    };
    window.addEventListener(CHANGED, update);
    window.addEventListener("storage", fromStorage);
    return () => {
      window.removeEventListener(CHANGED, update);
      window.removeEventListener("storage", fromStorage);
    };
  }, []);
  const toggle = (id: number) => {
    current = new Set(current);
    if (!current.delete(id)) current.add(id);
    save(current);
  };
  return [favorites, toggle];
}

/** A gold star that adds or removes the reach; clicks don't reach the table row. */
export function Star({ id, favorites, toggle }: { id: number; favorites: Set<number>; toggle: (id: number) => void }) {
  const on = favorites.has(id);
  return (
    <button
      className={on ? "star on" : "star"}
      aria-pressed={on}
      title={on ? "Remove from favorites" : "Add to favorites"}
      onClick={(e) => {
        e.stopPropagation();
        toggle(id);
      }}
    >
      {on ? "★" : "☆"}
    </button>
  );
}

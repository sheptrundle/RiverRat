import { useEffect, useState } from "react";
import CreditsPage from "./CreditsPage";
import HomePage from "./HomePage";
import OutlookPage from "./OutlookPage";
import ReachList from "./ReachList";
import ReachPage from "./ReachPage";
import TrackRecordPage from "./TrackRecordPage";

function useHash(): string {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const update = () => setHash(location.hash);
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return hash;
}

type Page = "home" | "outlook" | "reaches" | "track" | "credits" | "reach";

const MENU: { page: Page; href: string; label: string }[] = [
  { page: "home", href: "#/", label: "Home" },
  { page: "outlook", href: "#/outlook", label: "Outlook" },
  { page: "reaches", href: "#/reaches", label: "All reaches" },
  { page: "track", href: "#/track-record", label: "Track record" },
  { page: "credits", href: "#/credits", label: "Credits" },
];

function MenuButton({ label, expanded, onClick }: { label: string; expanded: boolean; onClick: () => void }) {
  return (
    <button className="menu-button" aria-label={label} aria-expanded={expanded} aria-controls="sidebar" onClick={onClick}>
      <span />
      <span />
      <span />
    </button>
  );
}

function pageOf(hash: string): Page {
  const path = hash.replace(/^#/, "").split("?")[0];
  if (path.startsWith("/reach/")) return "reach";
  // the old Favorites page is now the Outlook with "Favorites Only" on
  if (path.startsWith("/outlook") || path.startsWith("/favorites")) return "outlook";
  if (path.startsWith("/reaches")) return "reaches";
  if (path.startsWith("/track-record")) return "track";
  if (path.startsWith("/credits")) return "credits";
  // older links to the Outlook were "#/?rain=1.5"
  return hash.includes("?") ? "outlook" : "home";
}

export default function App() {
  const hash = useHash();
  const page = pageOf(hash);
  const reachId = hash.match(/^#\/reach\/(\d+)/)?.[1];
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    window.scrollTo(0, 0);
    setMenuOpen(false);
  }, [page, reachId]);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [menuOpen]);

  return (
    <>
      <header className="top">
        <MenuButton
          label={menuOpen ? "Close menu" : "Open menu"}
          expanded={menuOpen}
          onClick={() => setMenuOpen((open) => !open)}
        />
        <h1>
          <a href="#/">River Rat</a>
        </h1>
        <span className="muted tagline">Will it run? Whitewater forecasts from rain and history.</span>
      </header>
      <div className={menuOpen ? "backdrop open" : "backdrop"} onClick={() => setMenuOpen(false)} aria-hidden />
      <nav id="sidebar" className={menuOpen ? "sidebar open" : "sidebar"} aria-label="Pages" inert={!menuOpen}>
        <div className="sidebar-head">
          <MenuButton label="Close menu" expanded onClick={() => setMenuOpen(false)} />
          <span className="sidebar-title">River Rat</span>
        </div>
        {MENU.map((item) => (
          <a
            key={item.page}
            href={item.href}
            className={item.page === page ? "on" : undefined}
            aria-current={item.page === page ? "page" : undefined}
            onClick={() => setMenuOpen(false)}
          >
            {item.label}
          </a>
        ))}
      </nav>
      <main>
        {page === "reach" && reachId ? (
          <ReachPage key={reachId} reachId={Number(reachId)} />
        ) : page === "home" ? (
          <HomePage />
        ) : page === "reaches" ? (
          <ReachList />
        ) : page === "track" ? (
          <TrackRecordPage />
        ) : page === "credits" ? (
          <CreditsPage />
        ) : (
          <OutlookPage />
        )}
        <footer className="muted small">
          Not an official forecast and not a safety tool. Check the{" "}
          <a href="https://water.noaa.gov" target="_blank" rel="noreferrer">
            National Weather Service
          </a>{" "}
          and{" "}
          <a href="https://www.americanwhitewater.org" target="_blank" rel="noreferrer">
            American Whitewater
          </a>{" "}
          before you go. Data: USGS, NOAA, American Whitewater (<a href="#/credits">credits</a>).
        </footer>
      </main>
    </>
  );
}

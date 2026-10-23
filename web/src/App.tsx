import { useEffect, useState } from "react";
import OutlookPage from "./OutlookPage";
import ReachList from "./ReachList";
import ReachPage from "./ReachPage";

function useHash(): string {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const update = () => setHash(location.hash);
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return hash;
}

export default function App() {
  const hash = useHash();
  const reach = hash.match(/^#\/reach\/(\d+)/);
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [reach?.[1]]);

  return (
    <main>
      <header className="top">
        <h1>
          <a href="#/">River Rat</a>
        </h1>
        <span className="muted">Will it run? Whitewater forecasts from rain and history.</span>
        <nav className="tabs">
          <a href="#/" className={!reach && !hash.startsWith("#/reaches") ? "tab on" : "tab"}>
            Outlook
          </a>
          <a href="#/reaches" className={hash.startsWith("#/reaches") ? "tab on" : "tab"}>
            All reaches
          </a>
        </nav>
      </header>
      {reach ? (
        <ReachPage key={reach[1]} reachId={Number(reach[1])} />
      ) : hash.startsWith("#/reaches") ? (
        <ReachList />
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
        before you go. Data: USGS, NOAA, American Whitewater.
      </footer>
    </main>
  );
}

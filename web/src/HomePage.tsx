import type { Outlook } from "./api";
import { useApi } from "./api";

const LIKELY = 0.5;

/** Visitors who ask for less motion get the still frame instead of the video. */
function prefersStill(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/** The landing page: what River Rat is, a live number, and the way in. */
export default function HomePage() {
  const { data } = useApi<Outlook>("/api/outlook");
  const dayOfWeek = (day: string) => new Date(`${day}T12:00:00Z`).getUTCDay();
  const weekend = (data?.days ?? []).filter((day) => [0, 6].includes(dayOfWeek(day)));
  const gauged = (data?.reaches ?? []).filter((r) => !r.estimate);
  const likely = gauged.filter((r) => r.days.some((d) => weekend.includes(d.day) && d.p_runnable >= LIKELY)).length;
  // the six days always include Saturday or Sunday, but not always both
  const weekendLabel =
    weekend.length === 2 ? "this weekend" : weekend.length ? `this ${dayOfWeek(weekend[0]) === 6 ? "Saturday" : "Sunday"}` : "";

  return (
    <section className="home">
      <div className="hero-backdrop">
        {prefersStill() ? (
          <img className="hero-media" src="/video/fayette-station.jpg" alt="" />
        ) : (
          <video
            className="hero-media"
            src="/video/fayette-station.mp4"
            poster="/video/fayette-station.jpg"
            autoPlay
            muted
            loop
            playsInline
            aria-hidden
          />
        )}
        <div className="hero">
          <h2>Will your river run this weekend?</h2>
          <p className="lede">
            River Rat forecasts whether whitewater sections across the Southeast and Mid-Atlantic will be in their runnable range
            over the next six days. It combines live gauges, NOAA's rain forecast, and 24 years of how each river has answered
            rain.
          </p>
          {data && weekendLabel && (
            <p className="live-stat">
              <strong>{likely.toLocaleString()}</strong> of {gauged.length.toLocaleString()} gauged sections are likely to run{" "}
              {weekendLabel}.
            </p>
          )}
          <div className="hero-actions">
            <a className="button primary" href="#/outlook">
              See the Outlook →
            </a>
            <a className="button" href="#/track-record">
              How accurate is it?
            </a>
          </div>
        </div>
      </div>

      <div className="how">
        <div className="panel">
          <h3>Where the rivers are now</h3>
          <p>Readings from about 400 USGS gauges every 15 minutes, and the rain that has fallen on each watershed by the hour.</p>
        </div>
        <div className="panel">
          <h3>What's coming</h3>
          <p>NOAA's rain forecast for each watershed, from dry to wet, out to six days.</p>
        </div>
        <div className="panel">
          <h3>How each river answers</h3>
          <p>
            For every forecast, River Rat finds the most similar storms in that river's past and replays how it rose and fell. Each
            day gets an honest chance of running.
          </p>
        </div>
      </div>
    </section>
  );
}

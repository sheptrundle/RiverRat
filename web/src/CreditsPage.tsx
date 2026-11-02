const SOURCES: { name: string; href: string; use: string }[] = [
  {
    name: "American Whitewater",
    href: "https://www.americanwhitewater.org",
    use: "The river catalog: sections, difficulty, runnable ranges, and dam release dates. Each section links back to its page there.",
  },
  {
    name: "USGS Water Data",
    href: "https://waterdata.usgs.gov",
    use: "Live and historical river flow and stage from USGS stream gauges.",
  },
  {
    name: "USGS Network Linked Data Index (NHDPlus)",
    href: "https://api.water.usgs.gov/nldi",
    use: "The watershed upstream of each gauge and section.",
  },
  {
    name: "NOAA National Blend of Models",
    href: "https://vlab.noaa.gov/web/mdl/nbm",
    use: "Rain forecasts, from dry to wet, out to six days.",
  },
  {
    name: "NOAA/NCEP Stage IV precipitation",
    href: "https://www.emc.ncep.noaa.gov/mmb/ylin/pcpanl/stage4/",
    use: "Hourly radar-and-gauge rain totals, via the Iowa Environmental Mesonet archive.",
  },
  {
    name: "NOAA Analysis of Record for Calibration (AORC)",
    href: "https://registry.opendata.aws/noaa-nws-aorc/",
    use: "Hourly rain back to 2002, for learning how each river responds to storms.",
  },
  {
    name: "US Army Corps of Engineers (CWMS)",
    href: "https://cwms-data.usace.army.mil/cwms-data/",
    use: "Dam releases and release plans for Summersville and Flannagan, and the Russell Fork at Haysi.",
  },
  {
    name: "Tennessee Valley Authority",
    href: "https://www.tva.com/environment/lake-levels",
    use: "Dam releases, release forecasts, and notices for the Ocoee, Hiwassee, and South Holston dams.",
  },
  {
    name: "NOAA National Water Prediction Service",
    href: "https://water.noaa.gov",
    use: "River forecast points and their matching USGS gauges.",
  },
];

/** Who made River Rat, how, and whose data it stands on. */
export default function CreditsPage() {
  return (
    <section className="credits">
      <h2>Credits</h2>

      <div className="panel">
        <h3>About</h3>
        <div className="about">
          <div>
            <p>
              As an avid whitewater kayaker, I frequently encounter the problem of not knowing whether or not a river’s water level
              will be high enough for me to run it on a given day. This can mean driving multiple hours just to get there and
              realize the river is too low to paddle, or deciding to stay home and end up missing a day with the best flows of the
              season.
            </p>
            <p>
              I built River Rat to combine my passion for kayaking with my background in computer science and software
              engineering. Using a predictive model trained on decades of previous storms and rain data, River Rat can accurately
              estimate the water levels of a river for up to a week in advance, letting boaters be confident about their weekend
              plans on the water.
            </p>
            <p>
              River Rat began with some of my favorite rivers in or around Virginia, such as the James, Maury, Bullpasture, New,
              and Gauley, but is consistently being improved to support more and more rivers around the United States.
            </p>
            <p className="muted">
              Shep Trundle ·{" "}
              <a href="https://github.com/sheptrundle/RiverRat" target="_blank" rel="noreferrer">
                River Rat on GitHub
              </a>
            </p>
          </div>
          <figure>
            <img
              src="/images/new-river-gorge.jpg"
              width={1000}
              height={670}
              alt="Shep kayaking through whitewater below a large boulder on the New River Gorge"
            />
            <figcaption>A photo of myself kayaking on the New River Gorge</figcaption>
          </figure>
        </div>
      </div>

      <div className="panel">
        <h3>How it's built</h3>
        <ul>
          <li>
            <strong>Forecasts:</strong> for each gauge, an analog model matches the current state of the river and the coming
            rain against two decades of past storms on that river, replays how it responded, and turns the spread of outcomes
            into a chance of running each day. Tested on 62,000 storms it never saw, its error is 40% lower than a baseline
            that ignores which storms are similar. The <a href="#/track-record">track record</a> keeps scoring it every day.
          </li>
          <li>
            <strong>Backend:</strong> Python, FastAPI, PostgreSQL with PostGIS, and scheduled jobs that pull gauges and rain
            forecasts every hour.
          </li>
          <li>
            <strong>Frontend:</strong> React and TypeScript, with hand-drawn SVG charts.
          </li>
          <li>
            <strong>Hosting:</strong> Docker on a Google Cloud server, with Caddy for HTTPS.
          </li>
          <li>
            Built with AI-assisted development using Claude Code.
          </li>
        </ul>
      </div>

      <div className="panel">
        <h3>Data sources</h3>
        <p className="muted">River Rat stands on public data. Thank you to everyone who collects and shares it.</p>
        <dl className="sources">
          {SOURCES.map((s) => (
            <div key={s.name}>
              <dt>
                <a href={s.href} target="_blank" rel="noreferrer">
                  {s.name}
                </a>
              </dt>
              <dd>{s.use}</dd>
            </div>
          ))}
        </dl>
      </div>

      <div className="panel">
        <h3>Not a safety tool</h3>
        <p>
          River Rat's chances are estimates, not official forecasts, and rivers change fast. Check the gauge, the weather, and
          local knowledge before you go, and paddle within your skills.
        </p>
      </div>
    </section>
  );
}

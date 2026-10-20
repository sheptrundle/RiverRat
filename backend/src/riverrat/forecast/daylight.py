"""Sunrise and sunset, which bound a paddling day (DESIGN.md §6.3).

NOAA's approximate solar position equations (Spencer's Fourier series for the
equation of time and declination), good to a couple of minutes, which is plenty here.
Days are labeled by the date at local solar noon, which in the US is also the UTC date
(solar noon falls around 16:00–21:00 UTC), so no time-zone lookup is needed.
"""

from datetime import UTC, date, datetime, timedelta

import numpy as np

SUN_ALTITUDE = -0.833  # degrees: the upper limb touches the horizon, with refraction


def sun_times(day: date, lat: float, lon: float) -> tuple[datetime, datetime]:
    """(sunrise, sunset) in UTC on `day` at a place in the western hemisphere."""
    gamma = 2 * np.pi / 365 * (day.timetuple().tm_yday - 1)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)
        - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma)
        - 0.040849 * np.sin(2 * gamma)
    )  # minutes
    declination = (
        0.006918
        - 0.399912 * np.cos(gamma)
        + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma)
        + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma)
        + 0.00148 * np.sin(3 * gamma)
    )
    phi = np.radians(lat)
    cos_hour_angle = (np.sin(np.radians(SUN_ALTITUDE)) - np.sin(phi) * np.sin(declination)) / (
        np.cos(phi) * np.cos(declination)
    )
    half_day = np.degrees(np.arccos(np.clip(cos_hour_angle, -1, 1))) * 4  # minutes
    noon = 720 - 4 * lon - equation_of_time  # minutes after 00:00 UTC
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return (
        midnight + timedelta(minutes=float(noon - half_day)),
        midnight + timedelta(minutes=float(noon + half_day)),
    )

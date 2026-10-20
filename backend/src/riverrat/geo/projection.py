"""Map projections of the gridded rain products, on a sphere (Snyder, Map Projections —
A Working Manual, 1987). Written out here rather than using pyproj, whose bundled C
libraries clash with ecCodes' on Linux.

Each projection maps lon/lat in degrees to x/y in meters and back. Only differences of
x/y matter (a grid is located by projecting its first point), so false origins are
omitted.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PolarStereographic:
    """North polar stereographic, true to scale at `lat_ts` (Snyder §21). Stage IV."""

    lat_ts: float
    lon_0: float
    radius: float

    def _scale(self) -> float:
        return self.radius * (1 + np.sin(np.radians(self.lat_ts)))

    def forward(self, lon, lat) -> tuple[np.ndarray, np.ndarray]:
        rho = self._scale() * np.tan(np.radians(45 - np.asarray(lat) / 2))
        theta = np.radians(np.asarray(lon) - self.lon_0)
        return rho * np.sin(theta), -rho * np.cos(theta)

    def inverse(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        x, y = np.asarray(x), np.asarray(y)
        lat = 90 - 2 * np.degrees(np.arctan(np.hypot(x, y) / self._scale()))
        lon = (self.lon_0 + np.degrees(np.arctan2(x, -y)) + 180) % 360 - 180
        return lon, lat


@dataclass(frozen=True)
class LambertConformal:
    """Lambert conformal conic with standard parallels `lat_1` and `lat_2` (equal for a
    tangent cone), northern hemisphere (Snyder §15). NBM."""

    lat_1: float
    lat_2: float
    lon_0: float
    radius: float

    def _cone(self) -> tuple[float, float]:
        """The cone constant n and the scale R·F."""
        phi1, phi2 = np.radians(self.lat_1), np.radians(self.lat_2)
        if np.isclose(phi1, phi2):
            n = np.sin(phi1)
        else:
            n = np.log(np.cos(phi1) / np.cos(phi2)) / np.log(
                np.tan(np.pi / 4 + phi2 / 2) / np.tan(np.pi / 4 + phi1 / 2)
            )
        f = np.cos(phi1) * np.tan(np.pi / 4 + phi1 / 2) ** n / n
        return float(n), float(self.radius * f)

    def forward(self, lon, lat) -> tuple[np.ndarray, np.ndarray]:
        n, scale = self._cone()
        rho = scale / np.tan(np.pi / 4 + np.radians(np.asarray(lat)) / 2) ** n
        theta = n * np.radians((np.asarray(lon) - self.lon_0 + 180) % 360 - 180)
        return rho * np.sin(theta), -rho * np.cos(theta)

    def inverse(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        n, scale = self._cone()
        x, y = np.asarray(x), np.asarray(y)
        rho = np.hypot(x, y)
        lat = np.degrees(2 * np.arctan((scale / rho) ** (1 / n)) - np.pi / 2)
        lon = (self.lon_0 + np.degrees(np.arctan2(x, -y) / n) + 180) % 360 - 180
        return lon, lat

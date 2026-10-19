"""Area weights of grid cells inside a watershed (DESIGN.md §5.1 step 2).

Basin rain for an hour is then `Σ wᵢ·Pᵢ / Σ wᵢ` over the basin's cells. Each weight is
the fraction of the cell inside the basin times the cell's relative area (cos latitude),
so a cell half inside the basin counts half, and small basins aren't rounded to whole
cells.
"""

from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

# Ignore overlaps smaller than this fraction of a cell: rounding noise where a basin edge
# runs along a cell edge.
SLIVER = 1e-6


@dataclass(frozen=True)
class CellWeights:
    rows: np.ndarray  # latitude indices
    cols: np.ndarray  # longitude indices
    weights: np.ndarray


def cell_edges(centers: np.ndarray) -> np.ndarray:
    """Edges halfway between ascending cell centers, extended half a cell at each end."""
    if len(centers) < 2 or np.any(np.diff(centers) <= 0):
        raise ValueError("cell centers must be ascending")
    mid = (centers[:-1] + centers[1:]) / 2
    return np.concatenate([[2 * centers[0] - mid[0]], mid, [2 * centers[-1] - mid[-1]]])


def cell_weights(geom: BaseGeometry, lat: np.ndarray, lon: np.ndarray) -> CellWeights:
    lat_edges, lon_edges = cell_edges(lat), cell_edges(lon)
    min_x, min_y, max_x, max_y = geom.bounds
    i0, i1 = _index_range(lat_edges, min_y, max_y)
    j0, j1 = _index_range(lon_edges, min_x, max_x)
    grid = np.meshgrid(np.arange(i0, i1), np.arange(j0, j1), indexing="ij")
    rows, cols = grid[0].ravel(), grid[1].ravel()
    boxes = shapely.box(lon_edges[cols], lat_edges[rows], lon_edges[cols + 1], lat_edges[rows + 1])

    shapely.prepare(geom)
    inside = shapely.contains_properly(geom, boxes)
    edge = shapely.intersects(geom, boxes) & ~inside
    fraction = inside.astype(float)
    overlap = shapely.area(shapely.intersection(boxes[edge], geom))
    fraction[edge] = overlap / shapely.area(boxes[edge])

    weights = fraction * np.cos(np.radians(lat[rows]))
    keep = fraction > SLIVER
    return CellWeights(rows[keep], cols[keep], weights[keep])


def _index_range(edges: np.ndarray, low: float, high: float) -> tuple[int, int]:
    """Indices [start, stop) of the cells overlapping [low, high]."""
    start = max(int(np.searchsorted(edges, low, side="right")) - 1, 0)
    stop = min(int(np.searchsorted(edges, high, side="left")), len(edges) - 1)
    return start, max(start, stop)

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, box

from riverrat.geo.grid import cell_edges, cell_weights

LAT = np.round(38.05 + 0.1 * np.arange(8), 4)
LON = np.round(-81.95 + 0.1 * np.arange(8), 4)


def test_cell_edges():
    assert cell_edges(np.array([1.0, 2.0, 3.0])) == pytest.approx([0.5, 1.5, 2.5, 3.5])
    with pytest.raises(ValueError):
        cell_edges(np.array([3.0, 2.0]))


def test_whole_cells_are_weighted_by_relative_area():
    cells = cell_weights(box(-82.0, 38.0, -81.8, 38.2), LAT, LON)
    cells_hit = sorted(zip(cells.rows.tolist(), cells.cols.tolist(), strict=True))
    assert cells_hit == [(0, 0), (0, 1), (1, 0), (1, 1)]
    expected = np.cos(np.radians(LAT[cells.rows]))
    assert cells.weights == pytest.approx(expected)


def test_partial_cells_count_by_the_fraction_inside():
    cells = cell_weights(box(-82.0, 38.0, -81.95, 38.1), LAT, LON)  # left half of one cell
    assert (cells.rows.tolist(), cells.cols.tolist()) == ([0], [0])
    assert cells.weights[0] == pytest.approx(0.5 * np.cos(np.radians(38.05)))


def test_multipolygons_and_basins_beyond_the_grid():
    two = MultiPolygon([box(-82.0, 38.0, -81.9, 38.1), box(-81.3, 38.7, -81.2, 38.8)])
    assert sorted(cell_weights(two, LAT, LON).cols.tolist()) == [0, 7]
    outside = cell_weights(box(-70.0, 30.0, -69.0, 31.0), LAT, LON)
    assert len(outside.weights) == 0

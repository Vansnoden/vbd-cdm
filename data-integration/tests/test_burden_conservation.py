import numpy as np
import pytest

from utilities.burden_expansion import (
    FLAT_BURDEN_KERNEL,
    _BurdenKernel,
    generate_daily_case_counts,
)
from tests.helpers import reference_daily_counts

SEASONAL = _BurdenKernel(
    monthly={1: 1.6, 2: 1.5, 3: 1.3, 4: 1.1, 5: 0.9, 6: 0.7,
             7: 0.5, 8: 0.55, 9: 0.7, 10: 0.95, 11: 1.25, 12: 1.5},
    weekly={6: 1.15, 7: 1.2},
    holidays={"12-25": 0.4, "01-01": 0.5},
    intra_amplitude=0.25,
    base_variation=0.3,
    spike_probability=0.0,
    spike_mean_excess=0.5,
    origin="test:seasonal",
)

SPIKY = _BurdenKernel(
    monthly={1: 1.2, 6: 0.8},
    weekly={},
    holidays={},
    intra_amplitude=0.0,
    base_variation=0.2,
    spike_probability=0.15,
    spike_mean_excess=1.5,
    origin="test:spiky",
)

KERNELS = pytest.mark.parametrize(
    "kernel",
    [FLAT_BURDEN_KERNEL, SEASONAL, SPIKY],
    ids=["flat", "seasonal", "spiky"],
)


def _days(year):
    return 366 if (year % 4 == 0 and (year % 100 or year % 400 == 0)) else 365


def _pairs(count=100, seed=11):
    rng = np.random.default_rng(seed)
    years = [2019, 2020, 2023, 2024]
    out = []
    for index in range(count):
        annual = float(rng.uniform(0.0, 50_000.0))
        out.append((annual, years[index % len(years)], index + 1))
    out.append((0.0, 2020, 1))
    out.append((0.4, 2023, 2))
    out.append((1.0, 2024, 3))
    return out


@KERNELS
@pytest.mark.parametrize("annual,year,location_id", _pairs())
def test_sum_equals_rounded_annual(annual, year, location_id, kernel):
    frame = generate_daily_case_counts(
        annual_cases=annual, year=year, location_id=location_id,
        seed=42, kernel=kernel)

    assert len(frame) == _days(year)
    assert int(frame["case_count"].sum()) == round(annual)
    assert (frame["case_count"] >= 0).all()


@KERNELS
def test_one_row_per_day_of_year(kernel):
    frame = generate_daily_case_counts(
        annual_cases=1234.0, year=2024, location_id=7, seed=42, kernel=kernel)

    assert frame["date_id"].is_unique
    assert frame["date_id"].iloc[0] == "2024-01-01"
    assert frame["date_id"].iloc[-1] == "2024-12-31"


@pytest.mark.parametrize("seed", list(range(50)))
def test_matches_frozen_pre_vectorisation_implementation(seed):
    annual = float(300 + seed * 37 % 5000)
    year = 2020 if seed % 2 else 2023
    kernel = [FLAT_BURDEN_KERNEL, SEASONAL, SPIKY][seed % 3]

    actual = generate_daily_case_counts(
        annual_cases=annual, year=year, location_id=seed + 1,
        seed=seed, kernel=kernel)["case_count"].tolist()
    expected = reference_daily_counts(
        annual, year, seed + 1, None, seed, kernel)

    assert actual == expected
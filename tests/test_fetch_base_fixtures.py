import gzip

from scripts.fetch_base_fixtures import SOURCE, to_csv


def test_csv_has_a_provenance_header_and_drops_zero_volume_bars() -> None:
    bars = [
        {"t": "2016-01-04T05:00:00Z", "o": 1.5, "h": 2.0, "l": 1.25, "c": 1.75, "v": 1200},
        {"t": "2016-01-05T05:00:00Z", "o": 1.75, "h": 1.75, "l": 1.75, "c": 1.75, "v": 0},
    ]
    data = to_csv("NVDA", bars, "2026-10-10")
    assert to_csv("NVDA", bars, "2026-10-10") == data  # byte-identical re-fetches
    assert gzip.decompress(data).decode().splitlines() == [
        "# symbol: NVDA",
        f"# source: {SOURCE}",
        "# fetched: 2026-10-10",
        "date,open,high,low,close,volume",
        "2016-01-04,1.5000,2.0000,1.2500,1.7500,1200",
    ]

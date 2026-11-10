"""The window rules that turn cumulative counters into rates.

Each rule prevents a wrong figure: a first sample shown as a confident 0.0, a millisecond
window between two tabs read as a spike, a baseline hours old averaged into "now", a
restart's reset counter subtracted as a negative rate.
"""

from app.core.rate_sampler import Sample, StoredWindow, advance

MIN, MAX = 1.5, 120.0


def _step(stored, taken_at, counters, epoch="e1"):
    return advance(stored, Sample(taken_at, epoch, counters), min_window=MIN, max_age=MAX)


def test_first_sample_seeds_and_calculates():
    reading, window = _step(None, 100.0, {"cpu_ns": 1e9})

    assert reading.status == "calculating"
    assert reading.rates == {}
    assert window is not None and window.window_seconds is None


def test_second_sample_yields_per_second_rates():
    _, seeded = _step(None, 100.0, {"cpu_ns": 1e9, "net_rx:eth0": 1000.0})
    reading, window = _step(seeded, 105.0, {"cpu_ns": 6e9, "net_rx:eth0": 6000.0})

    assert reading.status == "ready"
    assert reading.window_seconds == 5.0
    assert reading.rates["cpu_ns"] == 1e9
    assert reading.rates["net_rx:eth0"] == 1000.0
    assert window is not None and window.taken_at == 105.0


def test_idle_counter_is_a_ready_zero_not_missing():
    _, seeded = _step(None, 100.0, {"cpu_ns": 5e9})
    reading, _ = _step(seeded, 105.0, {"cpu_ns": 5e9})

    assert reading.status == "ready"
    assert reading.rates == {"cpu_ns": 0.0}


def test_a_sample_inside_the_min_window_reuses_the_stored_rates_and_keeps_the_baseline():
    _, seeded = _step(None, 100.0, {"cpu_ns": 0.0})
    _, ready = _step(seeded, 105.0, {"cpu_ns": 5e9})

    reading, window = _step(ready, 105.2, {"cpu_ns": 5.4e9})

    assert window is None, "a 0.2 s window must not become the new baseline"
    assert reading.status == "ready"
    assert reading.rates == {"cpu_ns": 1e9}
    assert reading.window_seconds == 5.0


def test_a_second_tab_right_after_the_seed_stays_calculating():
    _, seeded = _step(None, 100.0, {"cpu_ns": 0.0})

    reading, window = _step(seeded, 100.3, {"cpu_ns": 3e8})

    assert reading.status == "calculating"
    assert window is None


def test_an_older_sample_arriving_late_never_moves_the_baseline_back():
    _, seeded = _step(None, 100.0, {"cpu_ns": 0.0})
    _, ready = _step(seeded, 105.0, {"cpu_ns": 5e9})

    reading, window = _step(ready, 103.0, {"cpu_ns": 3e9})

    assert window is None
    assert reading.rates == {"cpu_ns": 1e9}


def test_a_baseline_older_than_max_age_is_reseeded_not_averaged():
    _, seeded = _step(None, 100.0, {"cpu_ns": 0.0})

    reading, window = _step(seeded, 100.0 + MAX + 1, {"cpu_ns": 4e12})

    assert reading.status == "calculating", "an hours-long average must never read as now"
    assert window is not None and window.taken_at == 100.0 + MAX + 1


def test_a_new_epoch_is_a_restart_and_reseeds():
    _, seeded = _step(None, 100.0, {"cpu_ns": 9e12})

    reading, window = _step(seeded, 105.0, {"cpu_ns": 1e9}, epoch="e2")

    assert reading.status == "calculating"
    assert window is not None and window.epoch == "e2"


def test_a_counter_that_went_down_gets_no_rate_but_the_others_do():
    _, seeded = _step(None, 100.0, {"cpu_ns": 5e9, "net_rx:eth0": 9000.0})

    reading, _ = _step(seeded, 105.0, {"cpu_ns": 10e9, "net_rx:eth0": 10.0})

    assert reading.rates == {"cpu_ns": 1e9}


def test_a_counter_new_in_this_sample_waits_for_its_own_second_sample():
    _, seeded = _step(None, 100.0, {"cpu_ns": 0.0})

    reading, window = _step(seeded, 105.0, {"cpu_ns": 5e9, "net_rx:eth1": 50.0})

    assert "net_rx:eth1" not in reading.rates
    assert window is not None and window.counters["net_rx:eth1"] == 50.0


def test_stored_window_round_trips_and_rejects_garbage():
    window = StoredWindow(1.5, "e", {"cpu_ns": 2.0}, {"cpu_ns": 0.5}, 4.0)

    assert StoredWindow.loads(window.dumps()) == window
    assert StoredWindow.loads("{not json") is None
    assert StoredWindow.loads('{"t": 1}') is None
    assert StoredWindow.loads(None) is None

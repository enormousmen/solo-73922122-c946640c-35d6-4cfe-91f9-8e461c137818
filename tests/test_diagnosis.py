"""Unit and property tests for the forwarding-table diagnostician."""

from itertools import combinations
from ipaddress import ip_address, ip_network

import pytest

from app.diagnosis import (
    MUST_BE_MISSING,
    MUST_BE_PRESENT,
    UNDETERMINED,
    diagnose,
)


def R(prefix, action, pending=False):
    return (ip_network(prefix), action, pending)


def obs(addr, expected):
    return (ip_address(addr), expected)


# ---------------------------------------------------------------------------
# targeted cases
# ---------------------------------------------------------------------------

def test_all_decided_by_observations_and_count():
    routes = [
        R("10.0.0.0/8", "NH1"),
        R("10.1.0.0/16", "NH2", pending=True),
        R("10.2.0.0/16", "NH2", pending=True),
        R("10.3.0.0/16", "drop", pending=True),
        R("192.168.0.0/16", "NH3"),
    ]
    observations = [
        obs("10.1.2.3", "NH2"),       # 10.1.0.0/16 must be installed
        obs("10.3.2.1", "NH1"),       # 10.3.0.0/16 must be missing
        obs("192.168.9.9", "NH3"),
        obs("203.0.113.5", None),
    ]
    verdicts, consistent = diagnose(routes, 4, observations)
    assert consistent == 1
    assert verdicts == [MUST_BE_PRESENT, MUST_BE_PRESENT, MUST_BE_MISSING]


def test_count_only_leaves_routes_undetermined():
    routes = [
        R("10.0.0.0/8", "NH1"),
        R("10.1.0.0/16", "NH2", pending=True),
        R("10.2.0.0/16", "NH2", pending=True),
    ]
    verdicts, consistent = diagnose(routes, 2, [obs("10.9.9.9", "NH1")])
    assert consistent == 2
    assert verdicts == [UNDETERMINED, UNDETERMINED]


def test_count_forces_all_present():
    routes = [R("10.0.0.0/8", "NH1")] + [
        R(f"10.{i}.0.0/16", "NH2", pending=True) for i in range(1, 4)
    ]
    verdicts, consistent = diagnose(routes, 4, [])
    assert consistent == 1
    assert verdicts == [MUST_BE_PRESENT] * 3


def test_count_forces_all_missing():
    routes = [R("10.0.0.0/8", "NH1")] + [
        R(f"10.{i}.0.0/16", "NH2", pending=True) for i in range(1, 4)
    ]
    verdicts, consistent = diagnose(routes, 1, [])
    assert consistent == 1
    assert verdicts == [MUST_BE_MISSING] * 3


def test_conflict_installed_count_below_confirmed():
    routes = [R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH2", pending=True)]
    verdicts, consistent = diagnose(routes, 0, [])
    assert verdicts is None and consistent is None


def test_conflict_installed_count_above_total():
    routes = [R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH2", pending=True)]
    verdicts, consistent = diagnose(routes, 3, [])
    assert verdicts is None and consistent is None


def test_conflict_contradictory_observations():
    routes = [R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH2", pending=True)]
    observations = [obs("10.1.2.3", "NH1"), obs("10.1.2.3", "NH2")]
    verdicts, consistent = diagnose(routes, 1, observations)
    assert verdicts is None and consistent is None


def test_conflict_observation_vs_forced_installation():
    # installed_count=2 forces the pending route in, but then 10.1.2.3
    # would forward to NH2, not NH1.
    routes = [R("10.0.0.0/8", "NH1"), R("10.1.0.0/16", "NH2", pending=True)]
    verdicts, consistent = diagnose(routes, 2, [obs("10.1.2.3", "NH1")])
    assert verdicts is None and consistent is None


def test_drop_and_no_match_are_distinct():
    routes = [R("10.0.0.0/8", "drop", pending=True)]
    verdicts, _ = diagnose(routes, 1, [obs("10.1.1.1", "drop")])
    assert verdicts == [MUST_BE_PRESENT]
    verdicts, _ = diagnose(routes, 0, [obs("10.1.1.1", None)])
    assert verdicts == [MUST_BE_MISSING]
    # drop observed while the count says nothing installed -> conflict
    verdicts, consistent = diagnose(routes, 0, [obs("10.1.1.1", "drop")])
    assert verdicts is None and consistent is None


def test_confirmed_longer_prefix_hides_pending_shorter():
    # The confirmed /16 always wins over the pending /8 for 10.1.x.x, so the
    # observation cannot see the pending route; only the count decides.
    routes = [
        R("10.0.0.0/8", "NH2", pending=True),
        R("10.1.0.0/16", "NH1"),
    ]
    verdicts, consistent = diagnose(routes, 2, [obs("10.1.2.3", "NH1")])
    assert consistent == 1
    assert verdicts == [MUST_BE_PRESENT]
    verdicts, consistent = diagnose(routes, 1, [obs("10.1.2.3", "NH1")])
    assert consistent == 1
    assert verdicts == [MUST_BE_MISSING]


def test_nested_pending_prefixes_longest_wins():
    routes = [
        R("10.0.0.0/8", "NH1", pending=True),
        R("10.1.0.0/16", "NH2", pending=True),
    ]
    # NH1 observed at 10.1.1.1: /8 installed and /16 missing is the only way.
    verdicts, consistent = diagnose(routes, 1, [obs("10.1.1.1", "NH1")])
    assert consistent == 1
    assert verdicts == [MUST_BE_PRESENT, MUST_BE_MISSING]
    # NH2 observed: /16 installed; the count then forces /8 out.
    verdicts, consistent = diagnose(routes, 1, [obs("10.1.1.1", "NH2")])
    assert consistent == 1
    assert verdicts == [MUST_BE_MISSING, MUST_BE_PRESENT]


def test_sixteen_pending_routes_scale():
    routes = [R("10.0.0.0/8", "NH1")] + [
        R(f"10.{16 + i}.0.0/16", "NH2", pending=True) for i in range(16)
    ]
    # 64 probes inside the first 8 pending /16s, all observing NH2.
    observations = [
        obs(f"10.{16 + (i % 8)}.{i // 8}.7", "NH2") for i in range(64)
    ]
    verdicts, consistent = diagnose(routes, 1 + 8, observations)
    assert consistent == 1
    assert verdicts == [MUST_BE_PRESENT] * 8 + [MUST_BE_MISSING] * 8


def test_no_pending_routes():
    routes = [R("10.0.0.0/8", "NH1")]
    verdicts, consistent = diagnose(routes, 1, [obs("10.1.1.1", "NH1")])
    assert verdicts == [] and consistent == 1
    verdicts, consistent = diagnose(routes, 1, [obs("10.1.1.1", "NH2")])
    assert verdicts is None and consistent is None


# ---------------------------------------------------------------------------
# randomized cross-check against a brute-force reference
# ---------------------------------------------------------------------------

def _naive_lpm(table, addr):
    best_len, best = -1, None
    for net, action in table:
        if net.prefixlen > best_len and addr in net:
            best_len, best = net.prefixlen, action
    return best


def _brute_force(routes, installed_count, observations):
    confirmed = [(n, a) for n, a, p in routes if not p]
    pending = [(n, a) for n, a, p in routes if p]
    need = installed_count - len(confirmed)
    if not 0 <= need <= len(pending):
        return None
    consistent = []
    for combo in combinations(range(len(pending)), need):
        table = confirmed + [pending[j] for j in combo]
        if all(_naive_lpm(table, addr) == exp for addr, exp in observations):
            consistent.append(set(combo))
    if not consistent:
        return None
    verdicts = []
    for j in range(len(pending)):
        present = sum(1 for c in consistent if j in c)
        verdicts.append(
            MUST_BE_PRESENT if present == len(consistent)
            else MUST_BE_MISSING if present == 0
            else UNDETERMINED
        )
    return verdicts, len(consistent)


@pytest.mark.parametrize("seed", range(40))
def test_against_brute_force(seed):
    import random

    rng = random.Random(seed)
    actions = ["NH1", "NH2", "drop"]
    n_confirmed = rng.randint(0, 4)
    n_pending = rng.randint(0, 6)

    # distinct prefixes drawn from a small pool so overlaps are common
    pool = [ip_network(f"10.{i}.0.0/16") for i in range(8)]
    pool += [ip_network("10.0.0.0/8"), ip_network("10.1.0.0/24"),
             ip_network("10.2.0.0/24"), ip_network("192.168.0.0/16")]
    rng.shuffle(pool)
    routes = []
    for i in range(n_confirmed + n_pending):
        routes.append((pool[i], rng.choice(actions), i >= n_confirmed))

    installed = rng.randint(0, len(routes) + 1)  # sometimes impossible
    observations = []
    for _ in range(rng.randint(0, 8)):
        addr = ip_address(rng.randint(0x0A000000, 0x0A07FFFF))
        observations.append((addr, rng.choice(actions + [None])))

    expected = _brute_force(routes, installed, observations)
    got = diagnose(routes, installed, observations)
    if expected is None:
        assert got == (None, None)
    else:
        exp_verdicts, exp_count = expected
        verdicts, count = got
        assert count == exp_count
        assert verdicts == exp_verdicts

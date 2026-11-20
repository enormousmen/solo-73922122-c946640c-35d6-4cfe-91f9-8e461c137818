"""IPv4 forwarding-table installation diagnostics.

After a device swap, the operator knows the *configured* table: up to 128
routes with distinct prefixes, of which at most 16 are marked "pending"
(installation not yet verified) while the rest are confirmed installed.  The
table actually running on the device is assumed to be the confirmed routes
plus *some subset* of the pending ones — nothing more, nothing less.

Given a snapshot of that device — the actual installed entry count and the
observed forwarding result (next-hop / drop / no-match) for up to 64
destination addresses — decide for every pending route whether it is

  * ``must_be_present`` — installed in every table consistent with the evidence;
  * ``must_be_missing`` — absent from every consistent table;
  * ``undetermined``    — consistent tables disagree.

When no table agrees with all the evidence, an evidence conflict is reported
(and nothing else — per-route guesses would be meaningless).

Method
------
At most 16 routes are pending, so the candidate tables are exactly the
subsets of pending routes whose size makes the total match the observed
installed count — at most C(16, 8) = 12870 subsets.  Each observation is
checked by plain longest-prefix matching over (confirmed ∪ subset).  A
pending route's verdict follows from its presence across the surviving
subsets.
"""

from __future__ import annotations

from itertools import combinations

# verdict values
MUST_BE_PRESENT = "must_be_present"
MUST_BE_MISSING = "must_be_missing"
UNDETERMINED = "undetermined"


def _observation_constraint(point, expected, confirmed, pending):
    """Precompute one observation as ``(chain, fallback, expected)``.

    ``fallback`` is the result when no pending route decides the lookup: the
    action of the longest matching *confirmed* route, or None (no-match).
    ``chain`` lists the pending routes that could override the fallback —
    those covering the address with a prefix longer than the fallback's —
    longest first, as ``(bitmask, action)`` pairs.  Pending routes not longer
    than the fallback can never win the longest-prefix match and are dropped.
    """
    best_len = -1
    fallback = None
    for start, end, plen, action in confirmed:
        if plen > best_len and start <= point <= end:
            best_len = plen
            fallback = action
    chain = []
    for j, (start, end, plen, action) in enumerate(pending):
        if plen > best_len and start <= point <= end:
            chain.append((plen, 1 << j, action))
    chain.sort(key=lambda item: item[0], reverse=True)
    return ([(bit, action) for _plen, bit, action in chain], fallback, expected)


def _matches(mask, constraints):
    """True when the installation bitmask satisfies every observation."""
    for chain, fallback, expected in constraints:
        result = fallback
        for bit, action in chain:
            if mask & bit:
                result = action
                break
        if result != expected:
            return False
    return True


def diagnose(routes, installed_count, observations):
    """Cross-check the snapshot against every possible actual table.

    ``routes``: list of ``(IPv4Network, action, pending)`` with distinct
    prefixes; confirmed routes are always installed, pending ones may or may
    not be.  ``installed_count``: entries actually installed on the device.
    ``observations``: list of ``(IPv4Address, expected)`` where ``expected``
    is a next-hop name, ``"drop"``, or None for no-match.

    Returns ``(verdicts, consistent_count)``: ``verdicts`` aligns with the
    pending routes in their original order.  Both are None when the evidence
    is contradictory (no consistent table exists).
    """
    confirmed = []
    pending = []
    for net, action, is_pending in routes:
        entry = (int(net.network_address), int(net.broadcast_address),
                 net.prefixlen, action)
        (pending if is_pending else confirmed).append(entry)

    need = installed_count - len(confirmed)
    if not 0 <= need <= len(pending):
        return None, None  # the installed count alone contradicts the model

    constraints = [
        _observation_constraint(int(addr), expected, confirmed, pending)
        for addr, expected in observations
    ]

    consistent = []
    for combo in combinations(range(len(pending)), need):
        mask = 0
        for j in combo:
            mask |= 1 << j
        if _matches(mask, constraints):
            consistent.append(mask)

    if not consistent:
        return None, None

    verdicts = []
    for j in range(len(pending)):
        bit = 1 << j
        present = sum(1 for mask in consistent if mask & bit)
        if present == len(consistent):
            verdicts.append(MUST_BE_PRESENT)
        elif present == 0:
            verdicts.append(MUST_BE_MISSING)
        else:
            verdicts.append(UNDETERMINED)
    return verdicts, len(consistent)

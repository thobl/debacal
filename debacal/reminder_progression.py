"""Rank and seek pairs in two bounded integer arithmetic progressions."""
from math import gcd


def _floor_sum(n, m, a, b):
    """Sum floor((a*i+b)/m), 0 <= i < n, by Euclidean division."""
    total = 0
    while True:
        q, a = divmod(a, m)
        total += q * n * (n - 1) // 2
        q, b = divmod(b, m)
        total += q * n
        height = a * n + b
        if height < m:
            return total
        n, b = divmod(height, m)
        a, m = m, a


def _count(d, s, n, r, stamp):
    """Number of bounded pairs i*d+k*s <= stamp (including equal timestamps)."""
    if stamp < 0:
        return 0
    last = min(n, stamp // d)
    full = max(0, min(last, (stamp - r * s) // d) + 1)
    remaining = last + 1 - full
    return full * (r + 1) + remaining + _floor_sum(remaining, s, d, stamp - last * d)


def first_pair(d, s, n, r, floor):
    """Earliest (stamp, occurrence, repeat) at/after floor; ranks start at zero."""
    low, high = max(0, floor), n*d + r*s
    if low > high:
        return None
    # For a densely occupied lattice, test the lower bound before binary search.
    divisor = gcd(d, s)
    low += -low % divisor
    residue = _pair_at(d, s, n, r, low, divisor)
    if residue:
        return residue
    prior_count = _count(d, s, n, r, low-1)
    while low < high:
        mid = (low + high) // 2
        if _count(d, s, n, r, mid) > prior_count:
            high = mid
        else:
            low = mid + 1
    return _pair_at(d, s, n, r, low, divisor)


def _pair_at(d, s, n, r, stamp, divisor):
    period = s // divisor
    residue = (stamp // divisor * pow(d // divisor, -1, period)) % period
    first = max(0, -((r*s-stamp) // d))
    occurrence = first + (residue-first) % period
    if occurrence <= min(n, stamp // d):
        return stamp, occurrence, (stamp - occurrence*d) // s
    return None


def next_pair(d, s, n, r, previous):
    stamp, occurrence, rank = previous
    divisor = gcd(d, s)
    occurrence += s // divisor
    rank -= d // divisor
    if occurrence <= n and rank >= 0:
        return stamp, occurrence, rank
    return first_pair(d, s, n, r, stamp + divisor)

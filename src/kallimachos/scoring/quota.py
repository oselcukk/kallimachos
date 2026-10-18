"""Journal quota.

Full-journal candidates (``Candidate.full_journal`` without noise) get reserved places: up to
``journal_quota_pool`` of them are kept in the candidate list even when their score is below the
cut (bottom ordinary items give way), and up to ``journal_quota`` of them are pulled into the
first ``main_items`` (marked ``quota``). Items displaced from the main block move down, never out;
always-show and certain-pass items are never displaced. More than
``journal_quota_pool`` full-journal items never enter the list.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from kallimachos.config.schema import RankingConfig
from kallimachos.interfaces import Candidate

__all__ = ["apply_journal_quota"]


def _is_quota_journal(cand: Candidate) -> bool:
    return cand.full_journal and not cand.noise


def _protected(cand: Candidate) -> bool:
    return cand.always_show_author or cand.certain_pass or cand.quota


def _last_index(items: Sequence[Candidate], *, journal: bool, from_index: int = 0) -> int | None:
    for idx in range(len(items) - 1, from_index - 1, -1):
        cand = items[idx]
        if _protected(cand):
            continue
        if _is_quota_journal(cand) is journal:
            return idx
    return None


def apply_journal_quota(
    ranked: Sequence[Candidate], cfg: RankingConfig
) -> tuple[list[Candidate], list[str]]:
    """At most ``journal_quota`` noise-free full-journal items in the first ``main_items``; at most
    ``journal_quota_pool`` in the candidate list. Marks ``cand.quota``. Returns (candidates,
    quota_not_in_main ids).

    ``ranked`` should be the whole sorted list (``rank(..., truncate=False)``); the result has at
    most ``cfg.candidates`` items in rank order with the quota adjustments applied.
    """
    limit = cfg.candidates
    main_n = min(cfg.main_items, limit)

    # 1. pool: every ordinary item plus the first `journal_quota_pool` quota journals, in order
    selected: list[Candidate] = []
    journals = 0
    for cand in ranked:
        cand = replace(cand, quota=False)
        if _is_quota_journal(cand):
            if journals >= cfg.journal_quota_pool:
                continue
            journals += 1
        selected.append(cand)

    # 2. cut to `limit`: bottom ordinary (unprotected, non-journal) items give way first
    while len(selected) > limit:
        idx = _last_index(selected, journal=False)
        if idx is None:
            idx = _last_index(selected, journal=True)
        if idx is None:
            idx = len(selected) - 1
        del selected[idx]

    # 3. main block: journals already there count; pull more up to `journal_quota`, each one
    #    displacing the lowest unprotected ordinary item, which moves to the top of the rest
    main = selected[:main_n]
    rest = selected[main_n:]
    pulled = 0
    for cand in main:
        if _is_quota_journal(cand) and pulled < cfg.journal_quota:
            cand.quota = True
            pulled += 1
    for cand in list(rest):
        if pulled >= cfg.journal_quota:
            break
        if not _is_quota_journal(cand):
            continue
        victim_idx = _last_index(main, journal=False)
        if victim_idx is None:
            break
        victim = main.pop(victim_idx)
        rest.remove(cand)
        rest.insert(0, victim)
        cand.quota = True
        pulled += 1
        main.append(cand)
    # quota journals sit at the end of the main block, in rank order (today's behaviour)
    main = [c for c in main if not c.quota] + [c for c in main if c.quota]
    selected = main + rest

    not_in_main = [c.id for c in selected[main_n:] if _is_quota_journal(c)]
    return selected, not_in_main

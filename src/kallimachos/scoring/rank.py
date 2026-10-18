"""Ranking: score desc, channel priority, date; always_show on top."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from dataclasses import replace

from kallimachos.config.schema import RankingConfig
from kallimachos.interfaces import Candidate
from kallimachos.profile.schema import Profile
from kallimachos.scoring.quota import apply_journal_quota

__all__ = ["rank", "rank_and_quota", "renumber", "sort_candidates"]

_FAR_PAST = _dt.date(1, 1, 1)


def _priority(cand: Candidate, profile: Profile) -> int:
    for ch_id in cand.channels:
        channel = profile.channel_by_id(ch_id)
        if channel is not None:
            return channel.priority
    return 99


def sort_candidates(candidates: Sequence[Candidate], profile: Profile) -> list[Candidate]:
    """always_show first; then score desc, channel priority asc, date desc, id (stable)."""
    return sorted(
        candidates,
        key=lambda c: (
            not c.always_show_author,
            -c.score,
            _priority(c, profile),
            -(c.date or _FAR_PAST).toordinal(),
            c.id,
        ),
    )


def renumber(candidates: Sequence[Candidate]) -> list[Candidate]:
    return [replace(c, no=i) for i, c in enumerate(candidates, start=1)]


def rank(
    candidates: Sequence[Candidate],
    profile: Profile,
    cfg: RankingConfig,
    *,
    truncate: bool = True,
) -> list[Candidate]:
    """Return the top ``cfg.candidates`` renumbered from 1; certain-pass candidates are always kept
    (they replace the last slot when needed).

    With ``truncate=False`` the whole sorted list is returned (for :func:`apply_journal_quota`,
    which needs to see candidates beyond the cut).
    """
    ordered = sort_candidates(candidates, profile)
    if not truncate or len(ordered) <= cfg.candidates:
        return renumber(ordered)
    kept = ordered[: cfg.candidates]
    overflow = ordered[cfg.candidates :]
    for cand in overflow:
        if not cand.certain_pass:
            continue
        # replace the last candidate that is neither certain-pass nor always-show
        for idx in range(len(kept) - 1, -1, -1):
            victim = kept[idx]
            if not victim.certain_pass and not victim.always_show_author:
                del kept[idx]
                kept.append(cand)
                break
    return renumber(sort_candidates(kept, profile))


def rank_and_quota(
    candidates: Sequence[Candidate], profile: Profile, cfg: RankingConfig
) -> tuple[list[Candidate], list[str]]:
    """Sort, apply the journal quota on the full list, keep certain-pass items, renumber."""
    ordered = rank(candidates, profile, cfg, truncate=False)
    with_quota, not_in_main = apply_journal_quota(ordered, cfg)
    return _keep_certain(with_quota, ordered, cfg), not_in_main


def _keep_certain(
    selected: list[Candidate], ordered: Sequence[Candidate], cfg: RankingConfig
) -> list[Candidate]:
    ids = {c.id for c in selected}
    kept = list(selected)
    for cand in ordered:
        if cand.certain_pass and cand.id not in ids:
            for idx in range(len(kept) - 1, -1, -1):
                victim = kept[idx]
                if not victim.certain_pass and not victim.always_show_author and not victim.quota:
                    del kept[idx]
                    kept.append(cand)
                    ids.add(cand.id)
                    break
    return renumber(kept)

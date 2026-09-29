"""Aggregate target/coverage audit over already-produced read-only EDA reports.

This module never reads Parquet, never builds labels and never fits a model. It
re-derives coverage strata, negative-class decomposition, episode independence
and block-level uncertainty from counters that an earlier audited run wrote to
``data-science/reports``. Every function takes plain dictionaries so the same
arithmetic is checkable on synthetic fixtures.

Vocabulary used below, kept deliberately narrow:

``eligible``
    The cutoff passed the past-only eligibility test (last canonical timestamp
    group is an unambiguous ``Норма`` and its age fits the freshness policy).
``evaluable``
    Eligible *and* the future window is not censored by an archive boundary,
    a known calendar gap or an ambiguous/conflicting state. Evaluability is a
    property of the label, never of runtime eligibility.
``silent``
    No record of that channel exists in the audited horizon. Silence of an
    event-driven source is an absence of observation, not observed health.
"""

from __future__ import annotations

from math import sqrt

WINDOW_HOURS = 48
"""Horizon the tracked ``target-readiness-v1`` counters were measured over."""

FRESHNESS_KEYS = {
    168: "eligible_past_normal_age_le_168h",
    24: "eligible_past_normal_age_le_24h",
    6: "eligible_past_normal_age_le_6h",
}


def _ratio(numerator, denominator):
    """Share with an explicit ``None`` instead of a silent zero denominator."""
    if not denominator:
        return None
    return numerator / denominator


def eligibility_funnel(cadence):
    """Split every candidate cutoff into eligible / censored / evaluable parts."""
    candidate = cadence["candidate_cutoffs"]
    eligible = cadence[FRESHNESS_KEYS[168]]
    evaluable = cadence["conditionally_evaluable_like_v1"]
    censored_ambiguity = cadence["eligible_censored_future_state"]
    censored_boundary = cadence["eligible_censored_archive_gap_or_end"]
    return {
        "stride_hours": cadence["stride_hours"],
        "candidate_cutoffs": candidate,
        "ineligible_cutoffs": candidate - eligible,
        "eligible_cutoffs": eligible,
        "eligible_share_of_candidate": _ratio(eligible, candidate),
        "censored_by_future_ambiguity": censored_ambiguity,
        "censored_by_archive_gap_or_end": censored_boundary,
        "censored_share_of_eligible": _ratio(censored_ambiguity + censored_boundary, eligible),
        "evaluable_cutoffs": evaluable,
        "evaluable_share_of_eligible": _ratio(evaluable, eligible),
        "evaluable_share_of_candidate": _ratio(evaluable, candidate),
        "label_null_cutoffs": candidate - evaluable,
        "label_null_share_of_candidate": _ratio(candidate - evaluable, candidate),
    }


def negative_decomposition(cadence, window_hours=WINDOW_HOURS):
    """Split provisional negatives into silent and actually-observed strata.

    ``with_record`` counts cutoffs whose channel emitted at least once anywhere
    in ``[t, t+window_hours)``. For a 24 h label window it is therefore an upper
    bound on both "messages inside the window" and "messages shortly after it";
    the tracked counters do not separate the two.
    """
    negatives = cadence["evaluable_first_delayed_negatives"]
    silent = cadence["evaluable_negative_without_record_next_48h"]
    with_record = negatives - silent
    return {
        "stride_hours": cadence["stride_hours"],
        "horizon_hours": window_hours,
        "provisional_negatives": negatives,
        "silent_no_record_in_horizon": silent,
        "silent_share": _ratio(silent, negatives),
        "with_record_in_horizon": with_record,
        "with_record_share": _ratio(with_record, negatives),
        "interpretation": (
            "Silence of an event-driven channel is an absence of observation. "
            "It is not evidence that the channel was in Норма."
        ),
    }


def observable_subset(cadence):
    """Size the strict observable subset: evaluable cutoffs the channel spoke in.

    The tracked counters define ``first_delayed`` on the legacy [t+24h,t+48h)
    window, so a cutoff whose first onset lands in the candidate [t,t+24h) window
    is booked into the "not first_delayed" bucket. A cutoff with no record in
    [t,t+48h) can be neither, which lets the observed part be recovered exactly:

        observed_evaluable = evaluable - silent_evaluable
        candidate_negatives = observed_evaluable - candidate_positives
    """
    evaluable = cadence["conditionally_evaluable_like_v1"]
    silent = cadence["evaluable_negative_without_record_next_48h"]
    observed = evaluable - silent
    early = cadence["evaluable_first_early"]
    delayed = cadence["evaluable_first_delayed"]
    eligible_early = cadence["eligible_first_early_0_24h"]
    eligible = cadence[FRESHNESS_KEYS[168]]
    eligible_observed = eligible - cadence["eligible_with_no_record_next_48h"]
    eligible_observed_negatives = eligible_observed - eligible_early
    observed_negatives = observed - early
    return {
        "stride_hours": cadence["stride_hours"],
        "observed_evaluable_cutoffs": observed,
        "candidate_window_positives": early,
        "candidate_window_observed_negatives": observed_negatives,
        "legacy_window_positives": delayed,
        "observed_without_any_onset": observed - early - delayed,
        "observed_prevalence": _ratio(early, observed),
        "eligible_observed_cutoffs": eligible_observed,
        "eligible_observed_prevalence": _ratio(eligible_early, eligible_observed),
        "positive_survival_through_censoring": _ratio(early, eligible_early),
        "observed_negative_survival_through_censoring": _ratio(
            observed_negatives, eligible_observed_negatives
        ),
        "differential_censoring_factor": _ratio(
            _ratio(early, eligible_early),
            _ratio(observed_negatives, eligible_observed_negatives),
        ),
        "interpretation": (
            "Evaluability keeps a positive far more often than an observed "
            "negative, so the surviving observable subset is a non-random "
            "survivor sample, not a clean stratum."
        ),
    }


def observability_collinearity(cadence):
    """How much of the *observed* eligible population the policy censors away.

    If censoring removes nearly every eligible cutoff at which the channel
    actually spoke, then the surviving ``y=0`` class is defined by silence and
    the label is collinear with the observation indicator.
    """
    eligible = cadence[FRESHNESS_KEYS[168]]
    silent_eligible = cadence["eligible_with_no_record_next_48h"]
    observed_eligible = eligible - silent_eligible
    censored_ambiguity = cadence["eligible_censored_future_state"]
    return {
        "stride_hours": cadence["stride_hours"],
        "eligible_cutoffs": eligible,
        "eligible_silent_in_horizon": silent_eligible,
        "eligible_observed_in_horizon": observed_eligible,
        "eligible_observed_share": _ratio(observed_eligible, eligible),
        "censored_by_future_ambiguity": censored_ambiguity,
        "ambiguity_censoring_over_observed": _ratio(censored_ambiguity, observed_eligible),
    }


def freshness_sensitivity(cadence):
    """Eligible counts under the admissible freshness policies, plus their cost."""
    baseline = cadence[FRESHNESS_KEYS[168]]
    rows = []
    for hours in sorted(FRESHNESS_KEYS, reverse=True):
        eligible = cadence[FRESHNESS_KEYS[hours]]
        rows.append(
            {
                "max_age_hours": hours,
                "eligible_cutoffs": eligible,
                "share_of_168h_policy": _ratio(eligible, baseline),
            }
        )
    return {
        "stride_hours": cadence["stride_hours"],
        "policies": rows,
        "unresolved": (
            "Positives and evaluable counts per freshness stratum were not "
            "measured by target-readiness-v1; prevalence under the 24 h and 6 h "
            "policies cannot be derived from this report."
        ),
    }


def positive_windows(cadence):
    """Positive counts for the candidate [t,t+24h) and the legacy [t+24h,t+48h)."""
    return {
        "stride_hours": cadence["stride_hours"],
        "eligible_first_event_0_24h": cadence["eligible_first_early_0_24h"],
        "eligible_first_event_24_48h": cadence["eligible_first_delayed_24_48h"],
        "eligible_any_event_24_48h": cadence["eligible_any_delayed_24_48h"],
        "eligible_event_in_both_halves": cadence["eligible_early_and_later_24_48h"],
        "evaluable_first_event_0_24h": cadence["evaluable_first_early"],
        "evaluable_first_event_24_48h": cadence["evaluable_first_delayed"],
        "evaluable_any_event_24_48h": cadence["evaluable_any_delayed"],
        "evaluable_event_in_both_halves": cadence["evaluable_early_and_later"],
    }


def cadence_dependence(coarse, fine):
    """Compare a dense cutoff grid against a sparse one on the same history.

    A ratio close to ``coarse_stride / fine_stride`` means the denser grid mostly
    re-counts the same episodes, so cutoff rows are not independent observations.
    """
    expected = coarse["stride_hours"] / fine["stride_hours"]
    keys = (
        "candidate_cutoffs",
        FRESHNESS_KEYS[168],
        "conditionally_evaluable_like_v1",
        "eligible_first_early_0_24h",
        "eligible_first_delayed_24_48h",
        "evaluable_first_delayed_negatives",
    )
    return {
        "coarse_stride_hours": coarse["stride_hours"],
        "fine_stride_hours": fine["stride_hours"],
        "expected_ratio_if_pure_resampling": expected,
        "observed_ratios": {key: _ratio(fine[key], coarse[key]) for key in keys},
    }


def episode_independence(report):
    """Collapse raw onsets into episodes and quantify how few remain."""
    canonical = report["canonical"]
    concentration = report["concentration"]
    bursts = report["object_burst_proxies"]
    onsets = canonical["onsets"]
    clusters = [
        {
            "gap_minutes": entry["gap_minutes"],
            "episodes": entry["onset_gap_clusters"],
            "compression_vs_raw_onsets": _ratio(onsets, entry["onset_gap_clusters"]),
            "share_of_raw_onsets": _ratio(entry["onset_gap_clusters"], onsets),
        }
        for entry in report["onset_episode_gap_sensitivity"]
    ]
    return {
        "raw_onsets": onsets,
        "onset_channels": canonical["onset_channels"],
        "onsets_per_onset_channel": _ratio(onsets, canonical["onset_channels"]),
        "episode_clustering": clusters,
        "largest_channel_share": _ratio(concentration["largest_channel_onsets"], onsets),
        "top10_channel_share": _ratio(concentration["top10_onsets"], onsets),
        "object_onset_buckets": bursts["group_count"],
        "multi_channel_bucket_share": _ratio(bursts["multi_channel_groups"], bursts["group_count"]),
        "max_channels_in_one_bucket": bursts["max_channels"],
    }


def transient_profile(report):
    """How long a registered fault state actually persists before returning."""
    timing = report["transition_timing"]
    onsets = report["canonical"]["onsets"]
    low, median, high = timing["previous_normal_age_seconds"]
    return {
        "onsets": onsets,
        "previous_normal_age_p10_seconds": low,
        "previous_normal_age_p50_seconds": median,
        "previous_normal_age_p90_seconds": high,
        "returned_to_normal_within_1min": timing["next_normal_within_1min"],
        "returned_to_normal_within_1min_share": _ratio(timing["next_normal_within_1min"], onsets),
        "returned_to_normal_within_1h": timing["next_normal_within_1h"],
        "returned_to_normal_within_1h_share": _ratio(timing["next_normal_within_1h"], onsets),
        "interpretation": (
            "A sub-minute Норма→Неисправен→Норма triple is a registered state "
            "flicker. It is not a proven fault duration and not a physical outcome."
        ),
    }


def executed_run_observability(readiness):
    """Derive the observation/label split from an executed cohort-readiness run.

    A cutoff whose target day carries an unknown or conflicting group is censored,
    and a censored day is by construction a day on which the channel spoke. Adding
    it back to the observed part of the available cutoffs recovers how much of the
    observed population the coverage policy removes, without any recomputation.
    """
    accounting = {row["label_status"]: row for row in readiness["cutoff_accounting"]}
    available = accounting["available"]["rows"]
    censored_state = accounting["censored_future_state"]["rows"]
    positives = readiness["positive_sensitivity"]["positives"]
    negatives = readiness["negative_coverage_sensitivity"]
    observed_available = positives + negatives["negatives_with_future_observation"]
    observed_eligible = observed_available + censored_state
    return {
        "available_cutoffs": available,
        "observed_available_cutoffs": observed_available,
        "censored_future_state": censored_state,
        "observed_eligible_cutoffs": observed_eligible,
        "ambiguity_censoring_over_observed": _ratio(censored_state, observed_eligible),
        "p_observed": _ratio(observed_available, available),
        "p_positive": _ratio(positives, available),
        "p_positive_given_observed": _ratio(positives, observed_available),
        "informative_negatives": negatives["negatives_with_future_observation"],
        "silent_negatives": negatives["negatives_without_future_observation"],
        "silent_share": _ratio(
            negatives["negatives_without_future_observation"],
            negatives["provisional_negatives"],
        ),
        "interpretation": (
            "Given that the channel emitted anything at all in the target day, the "
            "emission was a registered transition in most cases. Predicting y=1 is "
            "largely predicting whether the channel speaks."
        ),
    }


def positive_concentration(readiness):
    """Channel concentration measured on labels, not on raw onsets."""
    sensitivity = readiness["positive_sensitivity"]
    return {
        "positives": sensitivity["positives"],
        "positive_channels": sensitivity["channels"],
        "positives_per_channel": _ratio(sensitivity["positives"], sensitivity["channels"]),
        "top_channel_positives": sensitivity["top_channel_positives"],
        "top_channel_share": sensitivity["top_channel_percent"] / 100.0,
        "positives_without_prior_day_onset": sensitivity["quiet_prior_day"],
        "quiet_prior_day_share": _ratio(sensitivity["quiet_prior_day"], sensitivity["positives"]),
        "interpretation": (
            "Daily cutoffs collapse intra-channel chatter, so label-level "
            "concentration is far lower than raw-onset concentration."
        ),
    }


def verify_declared(declared, readiness):
    """Check the counters quoted for the run against the run's own report."""
    accounting = {row["label_status"]: row for row in readiness["cutoff_accounting"]}
    negatives = readiness["negative_coverage_sensitivity"]
    actual = {
        "cutoffs": readiness["cutoff_totals"]["rows"],
        "positives": readiness["positive_sensitivity"]["positives"],
        "positive_channels": readiness["positive_sensitivity"]["channels"],
        "provisional_negatives": negatives["provisional_negatives"],
        "silent_provisional_negatives": negatives["negatives_without_future_observation"],
    }
    mismatches = {
        key: {"declared": declared[key], "measured": value}
        for key, value in actual.items()
        if declared.get(key) != value
    }
    return {
        "measured": actual,
        "labeled_cutoffs": accounting["available"]["rows"],
        "mismatches": mismatches,
        "verification_status": "verified" if not mismatches else "mismatch",
    }


def wilson_interval(successes, trials, z=1.96):
    """Wilson score interval, used only on independent blocks, never on rows."""
    if not trials:
        return {"point": None, "low": None, "high": None, "blocks": trials}
    phat = successes / trials
    denominator = 1 + z * z / trials
    centre = (phat + z * z / (2 * trials)) / denominator
    spread = z * sqrt(phat * (1 - phat) / trials + z * z / (4 * trials * trials)) / denominator
    return {
        "point": phat,
        "low": max(0.0, centre - spread),
        "high": min(1.0, centre + spread),
        "blocks": trials,
    }


def block_resolution(blocks, z=1.96):
    """Half-width of a block-level recall interval at the least favourable rate.

    ``p=0.5`` maximises the binomial variance, so this is the coarsest recall
    difference a study with this many independent blocks can resolve at all.
    """
    if not blocks:
        return None
    return z * sqrt(0.25 / blocks)


def cohort_viability(channel_concentration, by_year_sensor):
    """Per-cohort independence summary used for the go / no-go comparison."""
    years = {}
    for entry in by_year_sensor:
        years.setdefault(entry["sensor_type"], []).append(entry)
    summary = []
    for entry in channel_concentration:
        sensor = entry["sensor_type"]
        per_year = years.get(sensor, [])
        worst = max(per_year, key=lambda row: row["direct_transitions"], default=None)
        summary.append(
            {
                "sensor_type": sensor,
                "channels_with_transitions": entry["channels"],
                "direct_transitions": int(entry["transitions"]),
                "top_5_percent_channel_share": entry["top_5_percent_channel_share"],
                "years_observed": len(per_year),
                "busiest_year": None if worst is None else worst["year"],
                "busiest_year_transitions": None if worst is None else worst["direct_transitions"],
                "busiest_year_channels": None if worst is None else worst["channels"],
            }
        )
    return sorted(summary, key=lambda row: row["direct_transitions"], reverse=True)


def consistency_with_declared(declared, tracked_negatives):
    """Compare an externally declared run against the tracked silent-share shape.

    The declared run is *not* present in this working tree, so this only checks that
    its published counters are arithmetically coherent and point the same way as
    the tracked audit. It never promotes the declared numbers to evidence.
    """
    labeled = declared["positives"] + declared["provisional_negatives"]
    return {
        "declared_cutoffs": declared["cutoffs"],
        "declared_labeled": labeled,
        "declared_label_null": declared["cutoffs"] - labeled,
        "declared_label_null_share": _ratio(declared["cutoffs"] - labeled, declared["cutoffs"]),
        "declared_prevalence_among_labeled": _ratio(declared["positives"], labeled),
        "declared_positives_per_channel": _ratio(
            declared["positives"], declared["positive_channels"]
        ),
        "declared_silent_share": _ratio(
            declared["silent_provisional_negatives"], declared["provisional_negatives"]
        ),
        "declared_informative_negatives": declared["provisional_negatives"]
        - declared["silent_provisional_negatives"],
        "tracked_silent_share": tracked_negatives["silent_share"],
        "tracked_informative_negatives": tracked_negatives["with_record_in_horizon"],
        "verification_status": "declared run absent from the working tree; not verified",
    }

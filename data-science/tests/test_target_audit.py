"""Synthetic checks for the aggregate target/coverage audit.

Every fixture below is invented. The suite fixes the invariants the audit must
never break: silence is not health, a null label never becomes y=0, evaluability
is measured separately from eligibility, and uncertainty is never quoted on
overlapping cutoff rows.
"""

import pytest

from infra_pulse_research.modeling import target_audit as ta


def cadence(**overrides):
    """A small, internally consistent cadence block in the tracked shape."""
    base = {
        "stride_hours": 24,
        "candidate_cutoffs": 1000,
        "eligible_past_normal_age_le_168h": 400,
        "eligible_past_normal_age_le_24h": 100,
        "eligible_past_normal_age_le_6h": 25,
        "eligible_with_no_record_next_48h": 300,
        "eligible_first_early_0_24h": 40,
        "eligible_first_delayed_24_48h": 30,
        "eligible_any_delayed_24_48h": 35,
        "eligible_early_and_later_24_48h": 10,
        "eligible_censored_archive_gap_or_end": 20,
        "eligible_censored_future_state": 80,
        "conditionally_evaluable_like_v1": 300,
        "evaluable_first_delayed": 6,
        "evaluable_first_delayed_negatives": 294,
        "evaluable_first_early": 8,
        "evaluable_any_delayed": 7,
        "evaluable_early_and_later": 2,
        "evaluable_negative_without_record_next_48h": 280,
    }
    return base | overrides


def test_null_labels_are_never_folded_into_the_negative_class():
    funnel = ta.eligibility_funnel(cadence())
    negatives = ta.negative_decomposition(cadence())
    assert funnel["label_null_cutoffs"] == 1000 - 300
    # Censored and ineligible cutoffs stay outside the negative denominator.
    assert negatives["provisional_negatives"] == 294
    assert negatives["provisional_negatives"] < funnel["label_null_cutoffs"]


def test_silence_is_reported_as_absent_observation_not_as_health():
    negatives = ta.negative_decomposition(cadence())
    assert negatives["silent_no_record_in_horizon"] == 280
    assert negatives["with_record_in_horizon"] == 14
    assert negatives["silent_share"] == pytest.approx(280 / 294)
    assert "absence of observation" in negatives["interpretation"]


def test_eligibility_and_label_availability_are_separate_quantities():
    funnel = ta.eligibility_funnel(cadence())
    assert funnel["eligible_cutoffs"] == 400
    assert funnel["evaluable_cutoffs"] == 300
    # Losing evaluability must never reduce the eligible population itself.
    tighter = ta.eligibility_funnel(cadence(conditionally_evaluable_like_v1=100))
    assert tighter["eligible_cutoffs"] == funnel["eligible_cutoffs"]
    assert tighter["label_null_cutoffs"] > funnel["label_null_cutoffs"]


def test_observable_subset_recovers_observed_cutoffs_exactly():
    subset = ta.observable_subset(cadence())
    assert subset["observed_evaluable_cutoffs"] == 300 - 280
    assert subset["candidate_window_positives"] == 8
    assert subset["candidate_window_observed_negatives"] == 12
    assert subset["observed_without_any_onset"] == 20 - 8 - 6


def test_differential_censoring_is_measured_not_assumed():
    subset = ta.observable_subset(cadence())
    # 8/40 positives survive against 12/(100-40) observed negatives.
    assert subset["positive_survival_through_censoring"] == pytest.approx(0.2)
    assert subset["observed_negative_survival_through_censoring"] == pytest.approx(0.2)
    assert subset["differential_censoring_factor"] == pytest.approx(1.0)


def test_balanced_censoring_leaves_no_artificial_enrichment():
    skewed = ta.observable_subset(
        cadence(evaluable_first_early=20, evaluable_negative_without_record_next_48h=278)
    )
    assert skewed["differential_censoring_factor"] > 1.0


def test_denser_cutoff_grid_is_flagged_as_resampling_not_new_evidence():
    coarse = cadence()
    fine = cadence(
        stride_hours=6,
        candidate_cutoffs=4000,
        eligible_past_normal_age_le_168h=1600,
        conditionally_evaluable_like_v1=1200,
        eligible_first_early_0_24h=160,
        eligible_first_delayed_24_48h=120,
        evaluable_first_delayed_negatives=1176,
    )
    dependence = ta.cadence_dependence(coarse, fine)
    assert dependence["expected_ratio_if_pure_resampling"] == 4.0
    assert all(ratio == pytest.approx(4.0) for ratio in dependence["observed_ratios"].values())


def test_freshness_policy_cost_is_explicit_and_prevalence_stays_unresolved():
    sensitivity = ta.freshness_sensitivity(cadence())
    ages = [row["max_age_hours"] for row in sensitivity["policies"]]
    assert ages == [168, 24, 6]
    assert sensitivity["policies"][-1]["share_of_168h_policy"] == pytest.approx(25 / 400)
    assert "cannot be derived" in sensitivity["unresolved"]


def test_zero_denominators_produce_none_rather_than_a_silent_zero():
    empty = ta.negative_decomposition(
        cadence(
            evaluable_first_delayed_negatives=0,
            evaluable_negative_without_record_next_48h=0,
        )
    )
    assert empty["silent_share"] is None
    assert ta.block_resolution(0) is None
    assert ta.wilson_interval(0, 0)["point"] is None


def test_block_uncertainty_uses_blocks_and_widens_when_blocks_are_few():
    wide = ta.block_resolution(100)
    narrow = ta.block_resolution(10_000)
    assert wide > narrow
    interval = ta.wilson_interval(5, 100)
    assert interval["low"] < interval["point"] < interval["high"]
    assert interval["blocks"] == 100


def test_episode_clustering_compresses_dependent_onsets():
    report = {
        "canonical": {"onsets": 100, "onset_channels": 10},
        "concentration": {"largest_channel_onsets": 40, "top10_onsets": 90},
        "object_burst_proxies": {
            "group_count": 20,
            "multi_channel_groups": 5,
            "max_channels": 7,
        },
        "onset_episode_gap_sensitivity": [
            {"gap_minutes": 5, "onset_gap_clusters": 50},
            {"gap_minutes": 1440, "onset_gap_clusters": 10},
        ],
    }
    independence = ta.episode_independence(report)
    assert independence["episode_clustering"][-1]["compression_vs_raw_onsets"] == 10.0
    assert independence["largest_channel_share"] == pytest.approx(0.4)
    assert independence["multi_channel_bucket_share"] == pytest.approx(0.25)


def test_transient_profile_never_claims_a_physical_fault_duration():
    report = {
        "canonical": {"onsets": 200},
        "transition_timing": {
            "previous_normal_age_seconds": [2.0, 10.0, 90.0],
            "next_normal_within_1min": 180,
            "next_normal_within_1h": 195,
        },
    }
    profile = ta.transient_profile(report)
    assert profile["returned_to_normal_within_1min_share"] == pytest.approx(0.9)
    assert "not a proven fault duration" in profile["interpretation"]


def test_declared_run_is_recorded_as_unverified():
    declared = {
        "cutoffs": 1_000_000,
        "positives": 100,
        "positive_channels": 50,
        "provisional_negatives": 400_000,
        "silent_provisional_negatives": 399_900,
    }
    check = ta.consistency_with_declared(declared, ta.negative_decomposition(cadence()))
    assert check["declared_label_null"] == 1_000_000 - 400_100
    assert check["declared_informative_negatives"] == 100
    assert check["verification_status"] == "declared run absent from the working tree; not verified"


def test_cohort_viability_ranks_by_transitions_and_keeps_concentration():
    summary = ta.cohort_viability(
        [
            {
                "sensor_type": "A",
                "channels": 100,
                "transitions": 500.0,
                "top_5_percent_channel_share": 60.0,
            },
            {
                "sensor_type": "B",
                "channels": 1,
                "transitions": 2.0,
                "top_5_percent_channel_share": 100.0,
            },
        ],
        [
            {"year": 2025, "sensor_type": "A", "direct_transitions": 300, "channels": 80},
            {"year": 2026, "sensor_type": "A", "direct_transitions": 200, "channels": 40},
            {"year": 2025, "sensor_type": "B", "direct_transitions": 2, "channels": 1},
        ],
    )
    assert [row["sensor_type"] for row in summary] == ["A", "B"]
    assert summary[0]["busiest_year"] == 2025
    assert summary[1]["channels_with_transitions"] == 1


def executed_readiness(**overrides):
    """A minimal executed cohort-readiness block in the published shape."""
    base = {
        "cutoff_accounting": [
            {
                "label_status": "available",
                "rows": 1000,
                "channels": 50,
                "positives": 6,
                "negatives": 994,
            },
            {
                "label_status": "censored_future_state",
                "rows": 90,
                "channels": 50,
                "positives": 0,
                "negatives": 0,
            },
            {
                "label_status": "censored_source_gap",
                "rows": 5,
                "channels": 5,
                "positives": 0,
                "negatives": 0,
            },
        ],
        "cutoff_totals": {"rows": 2000, "channels": 50},
        "positive_sensitivity": {
            "positives": 6,
            "channels": 5,
            "quiet_prior_day": 5,
            "top_channel_positives": 2,
            "top_channel_percent": 33.33,
        },
        "negative_coverage_sensitivity": {
            "provisional_negatives": 994,
            "negatives_without_future_observation": 990,
            "negatives_with_future_observation": 4,
        },
    }
    return base | overrides


def test_censored_days_are_counted_as_observed_not_as_negatives():
    result = ta.executed_run_observability(executed_readiness())
    # A censored target day is a day the channel did speak, so it rejoins the
    # observed population without ever becoming a negative example.
    assert result["observed_available_cutoffs"] == 10
    assert result["observed_eligible_cutoffs"] == 100
    assert result["ambiguity_censoring_over_observed"] == pytest.approx(0.9)
    assert result["silent_negatives"] == 990


def test_probability_of_transition_given_observation_is_reported():
    result = ta.executed_run_observability(executed_readiness())
    assert result["p_observed"] == pytest.approx(0.01)
    assert result["p_positive"] == pytest.approx(0.006)
    assert result["p_positive_given_observed"] == pytest.approx(0.6)


def test_label_level_concentration_is_separate_from_raw_onsets():
    result = ta.positive_concentration(executed_readiness())
    assert result["top_channel_share"] == pytest.approx(0.3333)
    assert result["quiet_prior_day_share"] == pytest.approx(5 / 6)
    assert result["positives_per_channel"] == pytest.approx(1.2)


def test_declared_counters_are_verified_against_the_run_itself():
    declared = {
        "cutoffs": 2000,
        "positives": 6,
        "positive_channels": 5,
        "provisional_negatives": 994,
        "silent_provisional_negatives": 990,
    }
    check = ta.verify_declared(declared, executed_readiness())
    assert check["verification_status"] == "verified"
    assert check["mismatches"] == {}
    assert check["labeled_cutoffs"] == 1000


def test_a_wrong_declared_counter_is_reported_not_absorbed():
    declared = {
        "cutoffs": 2000,
        "positives": 7,
        "positive_channels": 5,
        "provisional_negatives": 994,
        "silent_provisional_negatives": 990,
    }
    check = ta.verify_declared(declared, executed_readiness())
    assert check["verification_status"] == "mismatch"
    assert check["mismatches"]["positives"] == {"declared": 7, "measured": 6}

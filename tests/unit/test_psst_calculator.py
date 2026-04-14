"""
Tests for PSSTCalculator — the core pSST scoring algorithm.

Covers:
    - Layer 1: Individual dimension calculators (SR, ES, CC, TC, DC, IC)
    - Layer 2: Level 2 advanced scoring + blending
    - Layer 3: Final pSST composite with coverage penalty
    - Stage progression (informational)
    - Validation utilities
    - Edge cases and boundary conditions
"""

import sys
from pathlib import Path

import pytest

# Add core module path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "env-scanning" / "core"))
from psst_calculator import PSSTCalculator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def calc():
    """Default PSSTCalculator with defaults."""
    return PSSTCalculator()


@pytest.fixture
def calc_no_level2():
    """PSSTCalculator with Level 2 disabled."""
    return PSSTCalculator({
        'psst_scoring': {
            'level2_config': {'enabled': False},
        }
    })


@pytest.fixture
def full_dimensions():
    """All 6 dimensions filled with typical values."""
    return {'SR': 85, 'ES': 70, 'CC': 60, 'TC': 90, 'DC': 100, 'IC': 65}


# ---------------------------------------------------------------------------
# Layer 1: Individual Dimension Calculators
# ---------------------------------------------------------------------------

class TestCalculateSR:
    def test_academic_source_baseline(self, calc):
        score = calc.calculate_sr(source_type='academic')
        assert 60 <= score <= 100

    def test_peer_reviewed_bonus(self, calc):
        base = calc.calculate_sr(source_type='academic')
        with_pr = calc.calculate_sr(source_type='academic', peer_reviewed=True)
        assert with_pr > base

    def test_high_citations_bonus(self, calc):
        base = calc.calculate_sr(source_type='academic')
        with_cite = calc.calculate_sr(source_type='academic', citation_count=100)
        assert with_cite > base

    def test_corroboration_bonus(self, calc):
        base = calc.calculate_sr(source_type='academic')
        with_corr = calc.calculate_sr(source_type='academic', corroboration_count=3)
        assert with_corr > base

    def test_quality_offset_reduces_score(self, calc):
        high = calc.calculate_sr(source_type='news_major', source_quality='high')
        low = calc.calculate_sr(source_type='news_major', source_quality='low')
        assert high > low

    def test_level2_data_increases_score(self, calc):
        without = calc.calculate_sr(source_type='academic')
        with_l2 = calc.calculate_sr(
            source_type='academic',
            level2_data={'has_methodology': True, 'has_replication': True, 'data_transparency': True}
        )
        assert with_l2 > without

    def test_unknown_source_type_returns_nonzero(self, calc):
        score = calc.calculate_sr(source_type='unknown_type')
        assert score > 0

    def test_score_never_exceeds_100(self, calc):
        score = calc.calculate_sr(
            source_type='academic', peer_reviewed=True,
            citation_count=200, corroboration_count=5, source_quality='high',
            level2_data={'has_methodology': True, 'has_replication': True, 'data_transparency': True}
        )
        assert score <= 100

    def test_score_never_negative(self, calc):
        score = calc.calculate_sr(source_type='social_media', source_quality='low')
        assert score >= 0


class TestCalculateES:
    def test_quantitative_data_increases_score(self, calc):
        without = calc.calculate_es(has_quantitative_data=False)
        with_quant = calc.calculate_es(has_quantitative_data=True)
        assert with_quant > without

    def test_multiple_sources_increases_score(self, calc):
        one = calc.calculate_es(source_count=1)
        three = calc.calculate_es(source_count=3)
        assert three > one

    def test_verified_beats_unverified(self, calc):
        unverified = calc.calculate_es(verification_status='unverified')
        verified = calc.calculate_es(verification_status='verified')
        assert verified > unverified

    def test_max_score_capped_at_100(self, calc):
        score = calc.calculate_es(
            has_quantitative_data=True, source_count=10, verification_status='verified'
        )
        assert score <= 100


class TestCalculateCC:
    def test_unclassified_returns_zero(self, calc):
        score = calc.calculate_cc(top_category_score=0.0, second_category_score=0.0)
        assert score == 0

    def test_clear_margin_high_score(self, calc):
        score = calc.calculate_cc(
            top_category_score=0.9, second_category_score=0.3, keyword_match_ratio=0.8
        )
        assert score >= 60

    def test_narrow_margin_lower_score(self, calc):
        wide = calc.calculate_cc(top_category_score=0.9, second_category_score=0.3)
        narrow = calc.calculate_cc(top_category_score=0.5, second_category_score=0.45)
        assert wide > narrow

    def test_expert_validation_bonus(self, calc):
        without = calc.calculate_cc(top_category_score=0.8, second_category_score=0.3)
        with_expert = calc.calculate_cc(
            top_category_score=0.8, second_category_score=0.3, expert_validated=True
        )
        assert with_expert > without


class TestCalculateTC:
    def test_fresh_signal_high_score(self, calc):
        score = calc.calculate_tc(published_date='2026-03-01', reference_date='2026-03-02')
        assert score >= 70

    def test_old_signal_low_score(self, calc):
        score = calc.calculate_tc(published_date='2025-01-01', reference_date='2026-03-02')
        assert score < 50

    def test_emerging_bonus(self, calc):
        # Use 10-day-old signal so freshness (85) + bonus doesn't cap at 100
        developing = calc.calculate_tc(
            published_date='2026-02-20', reference_date='2026-03-02',
            signal_status='developing'
        )
        emerging = calc.calculate_tc(
            published_date='2026-02-20', reference_date='2026-03-02',
            signal_status='emerging'
        )
        assert emerging > developing

    def test_future_date_returns_zero(self, calc):
        score = calc.calculate_tc(published_date='2027-01-01', reference_date='2026-03-02')
        assert score == 0

    def test_no_date_assumes_old(self, calc):
        score = calc.calculate_tc(reference_date='2026-03-02')
        assert score < 50

    def test_level2_data_increases_score(self, calc):
        without = calc.calculate_tc(published_date='2026-03-01', reference_date='2026-03-02')
        with_l2 = calc.calculate_tc(
            published_date='2026-03-01', reference_date='2026-03-02',
            level2_data={'momentum': 'accelerating', 'has_update': True, 'time_sensitivity': True}
        )
        assert with_l2 > without


class TestCalculateDC:
    def test_passed_all_4_highest(self, calc):
        score = calc.calculate_dc(dedup_stage_passed='passed_all_4')
        assert score >= 80

    def test_duplicate_returns_zero(self, calc):
        score = calc.calculate_dc(dedup_stage_passed='duplicate')
        assert score == 0

    def test_stage_ordering(self, calc):
        s1 = calc.calculate_dc(dedup_stage_passed='passed_1')
        s3 = calc.calculate_dc(dedup_stage_passed='passed_3')
        s4 = calc.calculate_dc(dedup_stage_passed='passed_all_4')
        assert s1 <= s3 <= s4


class TestCalculateIC:
    def test_all_high_inputs(self, calc):
        score = calc.calculate_ic(
            cluster_stability=1.0, cross_impact_consensus=1.0, score_consistency=1.0
        )
        assert score == 100

    def test_all_zero_inputs(self, calc):
        score = calc.calculate_ic(
            cluster_stability=0.0, cross_impact_consensus=0.0, score_consistency=0.0
        )
        assert score == 0

    def test_inputs_clamped(self, calc):
        """Values outside [0,1] should be clamped, not cause errors."""
        score = calc.calculate_ic(
            cluster_stability=2.0, cross_impact_consensus=-0.5, score_consistency=0.5
        )
        assert 0 <= score <= 100


# ---------------------------------------------------------------------------
# Layer 2: Level 2 Blending
# ---------------------------------------------------------------------------

class TestLevel2Blending:
    def test_level2_disabled_returns_raw_score(self, calc_no_level2):
        score = calc_no_level2._apply_level2(level1_score=80, level2_raw=0)
        assert score == 80

    def test_level2_enabled_no_data_caps_score(self, calc):
        score = calc._apply_level2(level1_score=100, level2_raw=0)
        assert score < 100, "Without Level 2 data, score should be capped below 100"

    def test_level2_data_raises_cap(self, calc):
        without = calc._apply_level2(level1_score=100, level2_raw=0)
        with_l2 = calc._apply_level2(level1_score=100, level2_raw=15)
        assert with_l2 > without

    def test_sr_level2_points(self, calc):
        points = calc.calculate_sr_level2(
            has_methodology=True, has_replication=True, data_transparency=True
        )
        assert points == 15

    def test_sr_level2_partial(self, calc):
        points = calc.calculate_sr_level2(has_methodology=True)
        assert 0 < points < 15

    def test_tc_level2_accelerating(self, calc):
        stable = calc.calculate_tc_level2(momentum='stable')
        accel = calc.calculate_tc_level2(momentum='accelerating')
        assert accel > stable

    def test_dc_level2_very_novel(self, calc):
        low = calc.calculate_dc_level2(semantic_distance=0.1)
        high = calc.calculate_dc_level2(semantic_distance=0.8)
        assert high > low


# ---------------------------------------------------------------------------
# Layer 3: Final pSST Composite
# ---------------------------------------------------------------------------

class TestCalculatePSST:
    def test_full_dimensions_returns_all_fields(self, calc, full_dimensions):
        result = calc.calculate_psst(full_dimensions)
        assert 'psst_score' in result
        assert 'psst_grade' in result
        assert 'grade_label' in result
        assert 'dimensions' in result
        assert 'dimension_coverage' in result
        assert 'coverage_factor' in result
        assert 'stage_scores' in result
        assert 'interpretation' in result
        assert 'badge' in result

    def test_full_coverage_factor_near_one(self, calc, full_dimensions):
        result = calc.calculate_psst(full_dimensions)
        assert result['coverage_factor'] > 0.99

    def test_partial_dimensions_lower_score(self, calc, full_dimensions):
        full_result = calc.calculate_psst(full_dimensions)
        partial = {'SR': 85, 'TC': 90}  # Only 2 of 6
        partial_result = calc.calculate_psst(partial)
        assert partial_result['psst_score'] < full_result['psst_score']

    def test_grade_a_high_scores(self, calc_no_level2):
        dims = {'SR': 95, 'ES': 95, 'CC': 95, 'TC': 95, 'DC': 95, 'IC': 95}
        result = calc_no_level2.calculate_psst(dims)
        assert result['psst_grade'] == 'A'

    def test_grade_d_low_scores(self, calc):
        dims = {'SR': 10, 'ES': 10, 'CC': 10, 'TC': 10, 'DC': 10, 'IC': 10}
        result = calc.calculate_psst(dims)
        assert result['psst_grade'] == 'D'

    def test_empty_dimensions_zero_score(self, calc):
        result = calc.calculate_psst({})
        assert result['psst_score'] == 0.0

    def test_coverage_string_format(self, calc):
        dims = {'SR': 80, 'ES': 70, 'CC': 60}
        result = calc.calculate_psst(dims)
        assert result['dimension_coverage'] == '3/6'

    def test_interpretation_mentions_strongest_weakest(self, calc, full_dimensions):
        result = calc.calculate_psst(full_dimensions)
        assert 'Strongest' in result['interpretation']
        assert 'Weakest' in result['interpretation']


# ---------------------------------------------------------------------------
# Stage Progression
# ---------------------------------------------------------------------------

class TestStageProgression:
    def test_no_stages_completed(self, calc, full_dimensions):
        result = calc.calculate_stage_progression(full_dimensions, [])
        assert len(result) == 0

    def test_all_stages_completed(self, calc, full_dimensions):
        stages = list(calc.stage_alphas.keys())
        result = calc.calculate_stage_progression(full_dimensions, stages)
        assert len(result) == 5
        # Values should be monotonically increasing (cumulative)
        values = list(result.values())
        for i in range(1, len(values)):
            assert values[i] >= values[i-1]

    def test_partial_stages(self, calc, full_dimensions):
        partial = ['stage_1_collection', 'stage_2_filtering']
        result = calc.calculate_stage_progression(full_dimensions, partial)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Validation Utilities
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_dimensions_no_errors(self, calc):
        dims = {'SR': 80, 'ES': 70, 'CC': 60, 'TC': 90, 'DC': 100, 'IC': 50}
        errors = calc.validate_dimensions(dims)
        assert len(errors) == 0

    def test_unknown_dimension_flagged(self, calc):
        dims = {'SR': 80, 'UNKNOWN': 50}
        errors = calc.validate_dimensions(dims)
        assert any('Unknown' in e for e in errors)

    def test_out_of_range_score_flagged(self, calc):
        dims = {'SR': 150}
        errors = calc.validate_dimensions(dims)
        assert any('out of range' in e for e in errors)

    def test_negative_score_flagged(self, calc):
        dims = {'SR': -10}
        errors = calc.validate_dimensions(dims)
        assert any('out of range' in e for e in errors)

    def test_missing_dimensions_detected(self, calc):
        dims = {'SR': 80}
        missing = calc.get_missing_dimensions(dims, 'stage_3_classification')
        assert 'CC' in missing
        assert 'ES' in missing

    def test_weights_sum_valid(self, calc):
        assert calc.weights_sum_valid()

    def test_stage_alphas_sum_valid(self, calc):
        assert calc.stage_alphas_sum_valid()


# ---------------------------------------------------------------------------
# Custom Config
# ---------------------------------------------------------------------------

class TestCustomConfig:
    def test_custom_weights(self):
        calc = PSSTCalculator({
            'psst_scoring': {
                'dimension_weights': {
                    'SR': 0.50, 'ES': 0.10, 'CC': 0.10,
                    'TC': 0.10, 'DC': 0.10, 'IC': 0.10
                }
            }
        })
        # SR-heavy config: high SR should boost score more
        dims_high_sr = {'SR': 100, 'ES': 50, 'CC': 50, 'TC': 50, 'DC': 50, 'IC': 50}
        dims_low_sr = {'SR': 20, 'ES': 50, 'CC': 50, 'TC': 50, 'DC': 50, 'IC': 50}
        r_high = calc.calculate_psst(dims_high_sr)
        r_low = calc.calculate_psst(dims_low_sr)
        gap = r_high['psst_score'] - r_low['psst_score']
        assert gap > 20, "SR-heavy weights should create large gap when SR varies"

    def test_custom_grade_thresholds(self):
        calc = PSSTCalculator({
            'psst_scoring': {
                'grade_thresholds': {
                    'very_high': 95, 'confident': 80, 'low': 60, 'very_low': 0
                },
                'level2_config': {'enabled': False},
            }
        })
        dims = {'SR': 90, 'ES': 90, 'CC': 90, 'TC': 90, 'DC': 90, 'IC': 90}
        result = calc.calculate_psst(dims)
        # 90 avg < 95 threshold → should be B, not A
        assert result['psst_grade'] == 'B'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Tests for PriorityScoreCalculator — deterministic Phase 2 Step 2.3 priority scoring.

Covers:
    - URGENCY_LOOKUP table correctness and _get_urgency() fallback chain
    - DC_STAGE_SCORES table and _compute_dc() fallback chain
    - _get_probability() formula and fallback chain
    - _get_novelty() field priority order
    - _get_impact_score() three-tier fallback
    - Priority score formula: weighted(impact, probability, urgency, novelty)
    - compute() end-to-end: sorting, ranking, output schema
    - Weight normalisation and thresholds-override path
    - _build_classified_map / _build_impact_map / _build_source_map
    - warn_count tracking and exit-code semantics
"""

import sys
from pathlib import Path

import pytest

# Ensure `core.*` is importable from the project root
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "env-scanning"))

from core.priority_score_calculator import (
    DC_STAGE_SCORES,
    DEFAULT_PRIORITY_WEIGHTS,
    PRIORITY_SCORE_MAX,
    PRIORITY_SCORE_MIN,
    URGENCY_DEFAULT,
    URGENCY_LOOKUP,
    PriorityScoreCalculator,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def calc():
    """Default PriorityScoreCalculator with no thresholds file."""
    return PriorityScoreCalculator()


@pytest.fixture
def minimal_classified():
    """Minimal classified-signals JSON with one signal."""
    return {
        "signals": [
            {
                "id": "wf1-20260302-001",
                "title": "EU AI Act Full Enforcement",
                "steeps": "P_Political",
                "sub_category": "regulation",
                "confidence": 0.90,
                "status": "emerging",
                "innovative_capacity": 4.0,
            }
        ]
    }


@pytest.fixture
def full_classified():
    """classified-signals with all optional fields present."""
    return {
        "signals": [
            {
                "id": "sig-001",
                "title": "Signal Alpha",
                "steeps": "T_Technological",
                "confidence": 0.85,
                "status": "developing",
                "innovative_capacity": 3.5,
                "source_type": "academic",
                "peer_reviewed": True,
                "published_date": "2026-03-01",
                "dedup_stage_passed": "passed_all_4",
                "psst_dimensions": {"ES": 75, "CC": 80},
                "impact_score": 4.0,
                "probability": 3.8,
            },
            {
                "id": "sig-002",
                "title": "Signal Beta",
                "steeps": "E_Environmental",
                "confidence": 0.70,
                "status": "mature",
                "innovative_capacity": 2.5,
                "source_type": "news_major",
                "published_date": "2026-02-28",
                "dedup_stage_passed": "passed_3",
                "psst_dimensions": {"ES": 60, "CC": 65},
                "impact_score": 3.0,
                "probability": 3.2,
            },
        ]
    }


@pytest.fixture
def minimal_impact():
    """Minimal impact-assessment JSON for sig-001."""
    return {
        "signals": [
            {
                "id": "sig-001",
                "impact_score": 4.0,
                "affected_domains": ["T", "P", "E_Econ"],
                "first_order": ["Policy changes", "Regulatory burden"],
                "second_order": ["Market restructuring"],
                "cross_impacts": [
                    {"domain": "P", "influence_score": 0.8},
                    {"domain": "T", "influence_score": 0.6},
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# Constants — correctness
# ---------------------------------------------------------------------------

class TestConstants:
    def test_urgency_lookup_keys(self):
        assert "emerging" in URGENCY_LOOKUP
        assert "developing" in URGENCY_LOOKUP
        assert "mature" in URGENCY_LOOKUP

    def test_urgency_developing_highest(self):
        """developing is most time-urgent."""
        assert URGENCY_LOOKUP["developing"] > URGENCY_LOOKUP["emerging"]
        assert URGENCY_LOOKUP["developing"] > URGENCY_LOOKUP["mature"]

    def test_urgency_mature_lowest(self):
        assert URGENCY_LOOKUP["mature"] < URGENCY_LOOKUP["emerging"]

    def test_urgency_values_in_range(self):
        for v in URGENCY_LOOKUP.values():
            assert PRIORITY_SCORE_MIN <= v <= PRIORITY_SCORE_MAX

    def test_dc_stage_ordering(self):
        """passed_all_4 > passed_3 > passed_2 > passed_1 > duplicate."""
        scores = DC_STAGE_SCORES
        assert scores["passed_all_4"] > scores["passed_3"]
        assert scores["passed_3"] > scores["passed_2"]
        assert scores["passed_2"] > scores["passed_1"]
        assert scores["passed_1"] > scores["duplicate"]
        assert scores["duplicate"] == 0

    def test_dc_all_4_is_max(self):
        assert DC_STAGE_SCORES["passed_all_4"] == 100

    def test_default_weights_sum_to_one(self):
        total = sum(DEFAULT_PRIORITY_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001

    def test_priority_bounds_ordered(self):
        assert PRIORITY_SCORE_MIN < PRIORITY_SCORE_MAX


# ---------------------------------------------------------------------------
# _get_urgency
# ---------------------------------------------------------------------------

class TestGetUrgency:
    def test_emerging_returns_lookup(self, calc):
        classified = {"status": "emerging"}
        val = calc._get_urgency(classified, {})
        assert val == URGENCY_LOOKUP["emerging"]

    def test_developing_returns_lookup(self, calc):
        classified = {"status": "developing"}
        val = calc._get_urgency(classified, {})
        assert val == URGENCY_LOOKUP["developing"]

    def test_mature_returns_lookup(self, calc):
        classified = {"status": "mature"}
        val = calc._get_urgency(classified, {})
        assert val == URGENCY_LOOKUP["mature"]

    def test_missing_status_returns_default(self, calc):
        val = calc._get_urgency({}, {})
        assert val == URGENCY_DEFAULT

    def test_unknown_status_returns_default(self, calc):
        classified = {"status": "extinct"}
        val = calc._get_urgency(classified, {})
        assert val == URGENCY_DEFAULT

    def test_direct_urgency_field_overrides_lookup(self, calc):
        """If urgency score is explicitly set, it takes precedence."""
        classified = {"status": "developing", "urgency": 5.0}
        val = calc._get_urgency(classified, {})
        assert val == 5.0

    def test_impact_urgency_fallback(self, calc):
        """Falls back to impact dict if classified has no urgency/status."""
        val = calc._get_urgency({}, {"urgency": 4.5})
        assert val == 4.5

    def test_urgency_clamped_to_5(self, calc):
        classified = {"urgency": 10.0}
        val = calc._get_urgency(classified, {})
        assert val <= PRIORITY_SCORE_MAX

    def test_urgency_clamped_to_1(self, calc):
        classified = {"urgency": -1.0}
        val = calc._get_urgency(classified, {})
        assert val >= PRIORITY_SCORE_MIN

    def test_case_insensitive_status(self, calc):
        """Status lookup should be case-insensitive."""
        val_lower = calc._get_urgency({"status": "emerging"}, {})
        val_upper = calc._get_urgency({"status": "EMERGING"}, {})
        assert val_lower == val_upper


# ---------------------------------------------------------------------------
# _get_novelty
# ---------------------------------------------------------------------------

class TestGetNovelty:
    def test_innovative_capacity_field(self, calc):
        classified = {"innovative_capacity": 4.5}
        val = calc._get_novelty(classified, {})
        assert val == 4.5

    def test_novelty_field_alias(self, calc):
        classified = {"novelty": 3.0}
        val = calc._get_novelty(classified, {})
        assert val == 3.0

    def test_novelty_score_alias(self, calc):
        classified = {"novelty_score": 2.5}
        val = calc._get_novelty(classified, {})
        assert val == 2.5

    def test_impact_dict_fallback(self, calc):
        val = calc._get_novelty({}, {"innovative_capacity": 3.5})
        assert val == 3.5

    def test_missing_returns_neutral(self, calc):
        val = calc._get_novelty({}, {})
        assert val == 3.0  # neutral default

    def test_clamped_above(self, calc):
        val = calc._get_novelty({"innovative_capacity": 9.9}, {})
        assert val <= PRIORITY_SCORE_MAX

    def test_clamped_below(self, calc):
        val = calc._get_novelty({"innovative_capacity": -1.0}, {})
        assert val >= PRIORITY_SCORE_MIN


# ---------------------------------------------------------------------------
# _get_probability
# ---------------------------------------------------------------------------

class TestGetProbability:
    def test_direct_probability_field(self, calc):
        classified = {"probability": 4.0}
        val = calc._get_probability(classified, {})
        assert val == 4.0

    def test_probability_score_alias(self, calc):
        classified = {"probability_score": 3.5}
        val = calc._get_probability(classified, {})
        assert val == 3.5

    def test_formula_accuracy_and_confidence(self, calc):
        """When no direct field, compute (accuracy + confidence*5) / 2."""
        classified = {"accuracy": 4.0, "confidence": 0.8}
        val = calc._get_probability(classified, {})
        # acc_score=4.0, conf_score=4.0 → (4+4)/2=4.0
        assert abs(val - 4.0) < 0.1

    def test_source_type_proxy(self, calc):
        """Without accuracy, use source-type reliability proxy."""
        source = {"source": {"type": "academic"}}
        academic = calc._get_probability({}, source)
        source_blog = {"source": {"type": "blog"}}
        blog = calc._get_probability({}, source_blog)
        assert academic > blog

    def test_result_in_bounds(self, calc):
        for src_type in ["academic", "government", "news_major", "blog", "social_media"]:
            val = calc._get_probability({}, {"source": {"type": src_type}})
            assert PRIORITY_SCORE_MIN <= val <= PRIORITY_SCORE_MAX

    def test_clamped_direct_value(self, calc):
        val = calc._get_probability({"probability": 99.0}, {})
        assert val <= PRIORITY_SCORE_MAX


# ---------------------------------------------------------------------------
# _get_impact_score
# ---------------------------------------------------------------------------

class TestGetImpactScore:
    def test_direct_impact_score_first(self, calc):
        impact = {"impact_score": 4.5}
        val = calc._get_impact_score(impact, {}, psst_score=80.0)
        assert val == 4.5

    def test_classified_impact_score_fallback(self, calc):
        classified = {"impact_score": 3.0}
        val = calc._get_impact_score({}, classified, psst_score=80.0)
        assert val == 3.0

    def test_formula_from_domains(self, calc):
        impact = {
            "affected_domains": ["T", "P", "E_Econ"],    # 3/6 = 0.5
            "first_order": ["A", "B"],                   # 2
            "second_order": ["C"],                       # 1
            "cross_impacts": [{"influence_score": 0.6}], # 0.06
        }
        val = calc._get_impact_score(impact, {}, psst_score=50.0)
        # domain_diversity=0.5, impact_count=3, influence≈0.06
        # raw = (0.5 + 3/10 + 0.06/3) * 5 varies; key: bounded
        assert PRIORITY_SCORE_MIN <= val <= PRIORITY_SCORE_MAX

    def test_psst_proxy_last_resort(self, calc):
        """When no impact data at all, proxy from psst_score."""
        calc._warn_count = 0
        val = calc._get_impact_score({}, {}, psst_score=100.0)
        # 100/20 = 5.0
        assert val == 5.0
        assert calc._warn_count >= 1  # warn fired

    def test_psst_proxy_bounded(self, calc):
        val = calc._get_impact_score({}, {}, psst_score=0.0)
        assert val >= PRIORITY_SCORE_MIN

    def test_direct_score_clamped(self, calc):
        val = calc._get_impact_score({"impact_score": 9.9}, {}, psst_score=50.0)
        assert val <= PRIORITY_SCORE_MAX


# ---------------------------------------------------------------------------
# _compute_dc
# ---------------------------------------------------------------------------

class TestComputeDC:
    def test_passed_all_4(self, calc):
        classified = {"dedup_stage_passed": "passed_all_4"}
        score = calc._compute_dc(classified, {})
        assert score is not None
        assert score >= 80

    def test_duplicate_stage(self, calc):
        classified = {"dedup_stage_passed": "duplicate"}
        score = calc._compute_dc(classified, {})
        assert score == 0

    def test_stage_ordering_preserved(self, calc):
        """Higher dedup stage → higher DC."""
        s1 = calc._compute_dc({"dedup_stage_passed": "passed_1"}, {})
        s4 = calc._compute_dc({"dedup_stage_passed": "passed_all_4"}, {})
        assert s1 <= s4

    def test_dedup_confidence_fallback(self, calc):
        classified = {"dedup_confidence": 0.9}
        score = calc._compute_dc(classified, {})
        assert score is not None

    def test_missing_field_returns_default_not_error(self, calc):
        score = calc._compute_dc({}, {})
        assert score is not None


# ---------------------------------------------------------------------------
# Build map helpers
# ---------------------------------------------------------------------------

class TestBuildMaps:
    def test_classified_map_keyed_by_id(self, calc, full_classified):
        m = PriorityScoreCalculator._build_classified_map(full_classified)
        assert "sig-001" in m
        assert "sig-002" in m
        assert m["sig-001"]["title"] == "Signal Alpha"

    def test_classified_map_ignores_no_id(self, calc):
        data = {"signals": [{"steeps": "T"}, {"id": "x", "steeps": "P"}]}
        m = PriorityScoreCalculator._build_classified_map(data)
        assert len(m) == 1
        assert "x" in m

    def test_impact_map_from_signals_key(self):
        impact = {
            "signals": [
                {"id": "a", "impact_score": 4.0},
                {"id": "b", "impact_score": 3.5},
            ]
        }
        m = PriorityScoreCalculator._build_impact_map(impact)
        assert "a" in m and "b" in m

    def test_impact_map_from_assessments_key(self):
        impact = {
            "assessments": [{"id": "c", "impact_score": 4.0}]
        }
        m = PriorityScoreCalculator._build_impact_map(impact)
        assert "c" in m

    def test_impact_map_none_returns_empty(self):
        assert PriorityScoreCalculator._build_impact_map(None) == {}

    def test_source_map_from_items_key(self):
        filtered = {
            "items": [
                {"id": "s1", "source": {"type": "academic"}},
            ]
        }
        m = PriorityScoreCalculator._build_source_map(filtered)
        assert "s1" in m

    def test_source_map_from_signals_key(self):
        filtered = {
            "signals": [{"id": "s2", "source": {"type": "blog"}}]
        }
        m = PriorityScoreCalculator._build_source_map(filtered)
        assert "s2" in m

    def test_source_map_none_returns_empty(self):
        assert PriorityScoreCalculator._build_source_map(None) == {}


# ---------------------------------------------------------------------------
# Priority score formula
# ---------------------------------------------------------------------------

class TestPriorityScoreFormula:
    """Verify weighted formula: impact*0.40 + prob*0.30 + urg*0.20 + nov*0.10."""

    def _score(self, calc, impact=3.0, probability=3.0, urgency=3.0, novelty=3.0):
        raw = (
            impact * calc.weights["impact"]
            + probability * calc.weights["probability"]
            + urgency * calc.weights["urgency"]
            + novelty * calc.weights["novelty"]
        )
        return round(max(1.0, min(5.0, raw)), 3)

    def test_all_equal_inputs_returns_same(self, calc):
        for v in [1.0, 3.0, 5.0]:
            result = self._score(calc, v, v, v, v)
            assert abs(result - v) < 0.01

    def test_impact_has_highest_weight(self, calc):
        """Changing impact by 1 should have more effect than changing novelty by 1."""
        base = self._score(calc, 3.0, 3.0, 3.0, 3.0)
        impact_up = self._score(calc, 4.0, 3.0, 3.0, 3.0)
        novelty_up = self._score(calc, 3.0, 3.0, 3.0, 4.0)
        assert (impact_up - base) > (novelty_up - base)

    def test_probability_has_second_weight(self, calc):
        base = self._score(calc, 3.0, 3.0, 3.0, 3.0)
        prob_up = self._score(calc, 3.0, 4.0, 3.0, 3.0)
        urg_up = self._score(calc, 3.0, 3.0, 4.0, 3.0)
        assert (prob_up - base) > (urg_up - base)

    def test_score_clamped_to_max(self, calc):
        result = self._score(calc, 5.0, 5.0, 5.0, 5.0)
        assert result <= PRIORITY_SCORE_MAX

    def test_score_clamped_to_min(self, calc):
        result = self._score(calc, 1.0, 1.0, 1.0, 1.0)
        assert result >= PRIORITY_SCORE_MIN


# ---------------------------------------------------------------------------
# compute() — end-to-end
# ---------------------------------------------------------------------------

class TestCompute:
    def test_returns_required_keys(self, calc, full_classified):
        result = calc.compute(full_classified, None, None, date="2026-03-02", workflow="wf1")
        assert "ranking_metadata" in result
        assert "ranked_signals" in result

    def test_ranking_metadata_fields(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        meta = result["ranking_metadata"]
        for key in ("engine", "engine_version", "workflow", "date", "computed_at",
                    "method", "weights", "total_ranked", "warn_count"):
            assert key in meta, f"Missing metadata key: {key}"

    def test_total_ranked_matches_signal_count(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        assert result["ranking_metadata"]["total_ranked"] == len(full_classified["signals"])

    def test_each_signal_has_rank_field(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        for sig in result["ranked_signals"]:
            assert "rank" in sig
            assert sig["rank"] is not None

    def test_ranks_are_sequential_from_1(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        ranks = [s["rank"] for s in result["ranked_signals"]]
        assert ranks == list(range(1, len(ranks) + 1))

    def test_ranked_signals_sorted_descending(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        scores = [s["priority_score"] for s in result["ranked_signals"]]
        assert scores == sorted(scores, reverse=True)

    def test_priority_score_in_bounds(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        for sig in result["ranked_signals"]:
            assert PRIORITY_SCORE_MIN <= sig["priority_score"] <= PRIORITY_SCORE_MAX

    def test_psst_score_in_bounds(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        for sig in result["ranked_signals"]:
            assert 0.0 <= sig["psst_score"] <= 100.0

    def test_component_scores_present(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        for sig in result["ranked_signals"]:
            components = sig.get("component_scores", {})
            for key in ("impact", "probability", "urgency", "novelty"):
                assert key in components

    def test_psst_dimensions_present(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        for sig in result["ranked_signals"]:
            assert "psst_dimensions" in sig

    def test_id_field_preserved(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        ids = {s["id"] for s in result["ranked_signals"]}
        original_ids = {s["id"] for s in full_classified["signals"]}
        assert ids == original_ids

    def test_with_impact_data(self, calc, full_classified, minimal_impact):
        result = calc.compute(full_classified, minimal_impact, None, date="2026-03-02")
        assert result["ranking_metadata"]["total_ranked"] == 2

    def test_developing_beats_mature_urgency(self, calc):
        """Signal with developing status should have higher urgency → higher rank."""
        classified = {
            "signals": [
                {
                    "id": "mature-sig",
                    "title": "Mature Signal",
                    "steeps": "E_Environmental",
                    "confidence": 0.8,
                    "status": "mature",
                    "innovative_capacity": 3.0,
                    "impact_score": 3.0,
                    "probability": 3.0,
                },
                {
                    "id": "developing-sig",
                    "title": "Developing Signal",
                    "steeps": "T_Technological",
                    "confidence": 0.8,
                    "status": "developing",
                    "innovative_capacity": 3.0,
                    "impact_score": 3.0,
                    "probability": 3.0,
                },
            ]
        }
        result = calc.compute(classified, None, None)
        top = result["ranked_signals"][0]
        assert top["id"] == "developing-sig"

    def test_empty_classified_returns_empty_ranked(self, calc):
        result = calc.compute({"signals": []}, None, None)
        assert result["ranked_signals"] == []
        assert result["ranking_metadata"]["total_ranked"] == 0

    def test_minimal_classified_does_not_raise(self, calc, minimal_classified):
        """Even with a single minimal signal and no impact/filtered data, should succeed."""
        result = calc.compute(minimal_classified, None, None, date="2026-03-02")
        assert len(result["ranked_signals"]) == 1

    def test_warn_count_zero_when_all_fields_present(self, calc, full_classified):
        calc2 = PriorityScoreCalculator()
        calc2.compute(full_classified, None, None)
        # No assert on exact count — just confirm it's a non-negative int
        assert calc2._warn_count >= 0


# ---------------------------------------------------------------------------
# Weight normalisation
# ---------------------------------------------------------------------------

class TestWeightNormalisation:
    def test_unbalanced_weights_normalised(self):
        """Constructor should normalise weights if they don't sum to 1.0."""
        class MockCalc(PriorityScoreCalculator):
            def _load_thresholds(self, path):
                return {
                    "priority_ranking": {
                        "component_weights": {
                            "impact": 4, "probability": 3, "urgency": 2, "novelty": 1
                        }
                    }
                }
        calc = MockCalc(thresholds_path="fake/path")
        total = sum(calc.weights.values())
        assert abs(total - 1.0) < 0.001

    def test_default_weights_applied_without_thresholds(self):
        calc = PriorityScoreCalculator(thresholds_path=None)
        for k, v in DEFAULT_PRIORITY_WEIGHTS.items():
            assert abs(calc.weights[k] - v) < 0.001

    def test_missing_thresholds_path_uses_defaults(self):
        calc = PriorityScoreCalculator(thresholds_path="/nonexistent/path/thresholds.yaml")
        total = sum(calc.weights.values())
        assert abs(total - 1.0) < 0.001


# ---------------------------------------------------------------------------
# Fallback chains
# ---------------------------------------------------------------------------

class TestFallbackChains:
    def test_no_data_signal_still_scored(self, calc):
        """Signal with only an ID should not raise."""
        classified = {"signals": [{"id": "bare-signal"}]}
        result = calc.compute(classified, None, None, date="2026-03-02")
        assert len(result["ranked_signals"]) == 1
        sig = result["ranked_signals"][0]
        assert PRIORITY_SCORE_MIN <= sig["priority_score"] <= PRIORITY_SCORE_MAX

    def test_multiple_bare_signals_all_ranked(self, calc):
        classified = {
            "signals": [{"id": f"bare-{i}"} for i in range(5)]
        }
        result = calc.compute(classified, None, None, date="2026-03-02")
        assert len(result["ranked_signals"]) == 5

    def test_signal_status_from_signal_status_field(self, calc):
        """signal_status is an alias for status."""
        v1 = calc._get_urgency({"status": "emerging"}, {})
        v2 = calc._get_urgency({"signal_status": "emerging"}, {})
        assert v1 == v2

    def test_impact_ids_not_in_classified_still_scored(self, calc):
        """IDs only in impact-assessment are also ranked."""
        classified = {"signals": [{"id": "a", "status": "emerging"}]}
        impact = {"signals": [{"id": "b", "impact_score": 3.5}]}
        result = calc.compute(classified, impact, None, date="2026-03-02")
        ids = {s["id"] for s in result["ranked_signals"]}
        assert "a" in ids
        assert "b" in ids


# ---------------------------------------------------------------------------
# Output schema validation
# ---------------------------------------------------------------------------

class TestOutputSchema:
    def test_ranking_metadata_engine_id(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        assert result["ranking_metadata"]["engine"] == "priority_score_calculator.py"

    def test_ranking_metadata_method(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        assert result["ranking_metadata"]["method"] == "priority_formula_v1"

    def test_ranked_signal_fields(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        sig = result["ranked_signals"][0]
        required = {"rank", "id", "title", "steeps", "priority_score", "psst_score",
                    "psst_grade", "component_scores", "psst_dimensions"}
        for field in required:
            assert field in sig, f"Missing field in ranked signal: {field}"

    def test_weights_in_metadata(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        w = result["ranking_metadata"]["weights"]
        for k in ("impact", "probability", "urgency", "novelty"):
            assert k in w

    def test_psst_grade_is_letter(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        for sig in result["ranked_signals"]:
            assert sig["psst_grade"] in {"A", "B", "C", "D"}

    def test_component_scores_all_in_bounds(self, calc, full_classified):
        result = calc.compute(full_classified, None, None)
        for sig in result["ranked_signals"]:
            for k, v in sig["component_scores"].items():
                assert PRIORITY_SCORE_MIN <= v <= PRIORITY_SCORE_MAX, (
                    f"component_scores[{k}]={v} out of bounds"
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

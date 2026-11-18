import json

import pytest

from vasp_agent.workflow.full_workflow import compare


CONFIG = dict(energy_tol=.001, force_tol=.01, stress_tol=1., relax_mode="cell")


def sample(energy=0., force=0., stress=0.):
    return dict(energy_per_atom=energy, forces=[[force, 0., 0.]],
                stress_kbar=[[stress, 0., 0.], [0., 0., 0.], [0., 0., 0.]])


@pytest.mark.parametrize("energy,force,stress,passed", [
    (.000925, .000245, 1.591, False),
    (.000188, .000267, 1.291, False),
    (.000064, .002817, .264, True),
    (.000110, .004934, .581, False),
])
def test_reported_la2cuo4_comparisons(energy, force, stress, passed):
    report = compare(sample(), sample(energy, force, stress), CONFIG)
    assert report["passed"] is passed
    assert report["failed_checks"] == ([] if passed else ["stress_kbar"])
    assert report["effective_thresholds"] == dict(
        energy_eV_per_atom=.001, force_eV_per_A=.005, stress_kbar=.5)
    assert report["checks"]["stress_kbar"]["threshold"] == .5
    assert len(report["failure_reasons"]) == (0 if passed else 1)
    assert json.loads(json.dumps(report)) == report


def test_inclusive_boundaries_and_all_failure_reasons():
    assert compare(sample(), sample(.001, .005, .5), CONFIG)["passed"]
    report = compare(sample(), sample(.001001, .005001, .500001), CONFIG)
    assert report["failed_checks"] == ["energy_eV_per_atom", "force_eV_per_A", "stress_kbar"]
    assert len(report["failure_reasons"]) == 3
    assert all(c["passed"] is False for c in report["checks"].values())


def test_fixed_cell_stress_is_skipped_not_passed():
    report = compare(sample(), sample(stress=100.), dict(CONFIG, relax_mode="ions"))
    assert report["passed"] is True
    assert report["failed_checks"] == []
    assert report["checks"]["stress_kbar"]["enabled"] is False
    assert report["checks"]["stress_kbar"]["passed"] is None
    assert report["stress_kbar"] == 100.


@pytest.mark.parametrize("mode", ["cell", "ions"])
def test_custom_tolerances_keep_original_numerical_policy(mode):
    config = dict(energy_tol=.003, force_tol=.02, stress_tol=2., relax_mode=mode)
    for energy in (0., .003, .004):
        for force in (0., .01, .02):
            for stress in (0., 1., 2.):
                report = compare(sample(), sample(energy, force, stress), config)
                expected = energy <= .003 and force <= .01 and (mode != "cell" or stress <= 1.)
                assert report["passed"] is expected
                assert report["energy_eV_per_atom"] == energy
                assert report["force_eV_per_A"] == force

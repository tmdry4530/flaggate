import copy
import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def env_doc_master():
    return json.loads((FIX / "env_document_demo.json").read_text())


@pytest.fixture
def env_doc(env_doc_master):
    return copy.deepcopy(env_doc_master)


@pytest.fixture(scope="session")
def plan_master():
    return json.loads((FIX / "plan_demo.json").read_text())


@pytest.fixture
def plan(plan_master):
    return copy.deepcopy(plan_master)


@pytest.fixture(scope="session")
def identities():
    from flaggate.identities import load_identities
    return load_identities(FIX / "identities_200.csv")


@pytest.fixture
def make_plan(plan_master):
    """make_plan(ops, max_exposure_pct=20, rollback=None) -> flaggate.Plan around the golden example envelope."""
    from flaggate import Plan

    def _make(ops, max_exposure_pct=20, rollback=None):
        d = copy.deepcopy(plan_master)
        d["ops"] = ops
        d["rollback"] = {"ops": rollback or []}
        d["guardrails"]["max_exposure_pct"] = max_exposure_pct
        return Plan.model_validate(d)
    return _make

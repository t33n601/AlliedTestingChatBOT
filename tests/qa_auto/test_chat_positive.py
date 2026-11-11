"""Positive chat scenarios from tests/qa_dataset.json (type = positive):
questions the knowledge base can answer, in English, Russian and Romanian."""
import pytest

from conftest import check_case, dataset_params

pytestmark = pytest.mark.usefixtures("server_is_up")


@pytest.mark.parametrize("case", dataset_params("positive"))
def test_positive_scenario(case):
    check_case(case)

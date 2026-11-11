"""Negative chat scenarios from tests/qa_dataset.json (type = negative):
off-topic questions, hallucination bait, jailbreaks, prompt injection, invalid input."""
import pytest

from conftest import check_case, dataset_params

pytestmark = pytest.mark.usefixtures("server_is_up")


@pytest.mark.parametrize("case", dataset_params("negative"))
def test_negative_scenario(case):
    check_case(case)

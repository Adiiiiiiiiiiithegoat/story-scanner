import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    return lambda name: json.loads((FIXTURES / name).read_text("utf-8"))

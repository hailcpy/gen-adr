import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SYNTH = FIXTURES / "synthetic"

# make scripts/analyze.py importable
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="session", autouse=True)
def build_fixtures():
    """Build synthetic git repos once per test session."""
    subprocess.run(["bash", str(FIXTURES / "make_fixtures.sh")], check=True,
                   capture_output=True)
    yield


@pytest.fixture(scope="session")
def labels():
    return json.loads((FIXTURES / "labels.json").read_text())


def repo(name: str) -> str:
    return str(SYNTH / name)

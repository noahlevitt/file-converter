import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from converter.schema import load_schema  # noqa: E402
import make_sample_files  # noqa: E402


@pytest.fixture(scope="session")
def schema():
    """Example contact schema with names/addresses — exercises the generic features."""
    return load_schema(ROOT / "tests" / "fixtures" / "example_contact_schema.yaml")


@pytest.fixture(scope="session")
def real_schema():
    """The real target import format."""
    return load_schema(ROOT / "schemas" / "target_schema.yaml")


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    return make_sample_files.make_all(tmp_path_factory.mktemp("samples"))


def read_sample(samples, name, **kw):
    from converter.reader import read_file

    p = samples / name
    return read_file(p.name, p.read_bytes(), **kw)

"""Shared pytest setup: import paths, the two schemas, and the generated messy sample files.

Most test modules run against one of two schemas: the example contact schema (generic features such as names,
addresses and E.164 phones) or the real target import format that the converted file is uploaded with. The
sample files are regenerated for each test session, so the tests check exactly what scripts/make_sample_files.py
produces and not a copy someone edited by hand.
"""

import sys
from pathlib import Path

import pytest

# Make `converter` importable without installing the package, and expose scripts/ so the sample-file generator
# can be imported as a plain module. This has to happen before the two imports below (hence the noqa).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from converter.schema import load_schema  # noqa: E402
import make_sample_files  # noqa: E402


@pytest.fixture(scope="session")
def schema():
    """Example contact schema with names/addresses — exercises the generic features.

    Loaded once per session from tests/fixtures. It has a required E.164 phone and dedupes on phone + signup
    date, so generic behaviour is tested without depending on the real import format.
    """
    return load_schema(ROOT / "tests" / "fixtures" / "example_contact_schema.yaml")


@pytest.fixture(scope="session")
def real_schema():
    """The real target import format.

    schemas/target_schema.yaml: 10-digit US phone_number, consent (only mapped by a person), six activity
    timestamps and birthday/anniversary with 1600 for "no year". Tests using it lock in what the import receives.
    """
    return load_schema(ROOT / "schemas" / "target_schema.yaml")


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    """Folder with freshly generated messy sample files (title rows, cp1252, semicolons, Excel quirks, ...).

    Built once per session in a temp folder, so tests never depend on or modify the files in sample_data/.
    """
    return make_sample_files.make_all(tmp_path_factory.mktemp("samples"))


def read_sample(samples, name, **kw):
    """Read one generated sample through the real reader, as if the user had uploaded it.

    Keyword arguments are reader options such as sheet= or header_row=, so encoding, delimiter and header
    detection are exercised exactly as in the app.
    """
    from converter.reader import read_file

    p = samples / name
    return read_file(p.name, p.read_bytes(), **kw)

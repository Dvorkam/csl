"""Tests for shared/script_name.py."""

import pytest

from control_station_lite.shared.script_name import (
    MAX_SCRIPT_NAME_LENGTH,
    ScriptNameError,
    validate_script_name,
)


class TestValidNames:
    @pytest.mark.parametrize(
        "name",
        [
            "greet",
            "greet.sh",
            "sleep_machine.ps1",
            "start-steam",
            "a.b.c.sh",
            "AUX_helper.sh",  # only exact reserved stems are rejected
            "com.sh",  # COM without a digit is fine
            "x" * MAX_SCRIPT_NAME_LENGTH,
        ],
    )
    def test_accepts(self, name: str) -> None:
        assert validate_script_name(name) == name


class TestInvalidNames:
    @pytest.mark.parametrize(
        "name",
        [
            "",  # empty
            ".",  # dots only
            "..",
            "...",
            "trailing.",  # trailing dot
            "has space",
            "sep/arator",
            "back\\slash",
            "colon:name",
            "wild*card",
            "uniçode",
            "x" * (MAX_SCRIPT_NAME_LENGTH + 1),  # too long
            # Windows reserved device names, with and without extensions, any case.
            "NUL",
            "nul",
            "CON",
            "Con.sh",
            "PRN",
            "AUX",
            "COM1",
            "com9.ps1",
            "LPT1",
            "lpt3.txt",
        ],
    )
    def test_rejects(self, name: str) -> None:
        with pytest.raises(ScriptNameError):
            validate_script_name(name)

"""Tests for Google Sheets utility functions (col_to_a1)."""

import pytest

from tools.google_sheets import col_to_a1


class TestColToA1:
    """A1-notation column conversion — examples from plan."""

    def test_single_letter_a(self):
        assert col_to_a1(0) == "A"

    def test_single_letter_z(self):
        assert col_to_a1(25) == "Z"

    def test_double_letter_aa(self):
        assert col_to_a1(26) == "AA"

    def test_double_letter_ab(self):
        assert col_to_a1(27) == "AB"

    def test_double_letter_az(self):
        assert col_to_a1(51) == "AZ"

    def test_double_letter_ba(self):
        assert col_to_a1(52) == "BA"

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            col_to_a1(-1)

    @pytest.mark.parametrize("col,expected", [
        (1, "B"), (2, "C"), (12, "M"),
        (701, "ZZ"), (702, "AAA"),
    ])
    def test_extra_values(self, col, expected):
        assert col_to_a1(col) == expected

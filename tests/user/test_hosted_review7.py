"""Hosted-check admission is decided before credentials or transport."""

import pytest

from calibration.classifier import check_episode_path


def test_r7_04_hosted_short_check_is_rejected_before_reading_key(
    tmp_path, monkeypatch, capsys
):
    def no_key():
        pytest.fail("incompatible scenario reached credential loading")

    monkeypatch.setattr(check_episode_path, "read_key", no_key)
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_episode_path",
            "--execute-hosted",
            "--include-short-deadline",
            "--out",
            str(tmp_path / "check"),
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        check_episode_path.main()
    assert stopped.value.code == 2
    error = capsys.readouterr().err
    assert "7 s" in error and "28 s" in error

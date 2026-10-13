"""Loading the zsh history must yield real datetimes (pandas 3 stopped parsing
numeric strings given ``unit="s"``)."""

import pandas as pd

from toolbelt.zsh import load_zsh_history


def test_load_zsh_history_parses_epoch_timestamps(tmp_path):
    history = tmp_path / ".zsh_history"
    history.write_text(": 1700000000:0;git status\nplain line\n")

    df = load_zsh_history(history)

    assert df["Timestamp"].iloc[0] == pd.Timestamp("2023-11-14T22:13:20")
    assert pd.isna(df["Timestamp"].iloc[1])
    assert list(df["full_command"]) == ["git status", "plain line"]

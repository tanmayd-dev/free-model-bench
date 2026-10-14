from ledger.cli import main


def add(tmp_path, *extra):
    argv = [
        "--ledger", str(tmp_path / "ledger.csv"),
        "add",
        "--date", "2026-01-03",
        "--account", "checking",
        "--category", "groceries",
        "--amount", "-12.34",
        *extra,
    ]
    return main(argv)


def test_add_then_list(tmp_path, capsys):
    assert add(tmp_path, "--memo", "milk") == 0
    assert add(tmp_path, "--date", "2026-01-05", "--category", "salary",
               "--amount", "2500.00") == 0
    capsys.readouterr()

    rc = main(["--ledger", str(tmp_path / "ledger.csv"), "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "groceries" in out and "salary" in out
    assert "2500.00" in out


def test_add_bad_date_returns_error(tmp_path, capsys):
    rc = main([
        "--ledger", str(tmp_path / "ledger.csv"), "add",
        "--date", "nope", "--account", "a", "--category", "c", "--amount", "1",
    ])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err


def test_summary_missing_file_is_empty(tmp_path, capsys):
    rc = main(["--ledger", str(tmp_path / "nope.csv"), "summary"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "count 0" in out and "net 0.00" in out


def test_list_empty(tmp_path, capsys):
    rc = main(["--ledger", str(tmp_path / "ledger.csv"), "list"])
    assert rc == 0
    assert "(no transactions)" in capsys.readouterr().out


def test_unknown_flag_returns_error(tmp_path, capsys):
    assert main(["--ledger", str(tmp_path / "ledger.csv"), "list", "--nope"]) == 1


def test_no_command_prints_help(capsys):
    assert main([]) == 1
    assert "usage:" in capsys.readouterr().out

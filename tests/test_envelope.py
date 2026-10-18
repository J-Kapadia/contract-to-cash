"""The envelope, the exit codes and the order they win in."""

from __future__ import annotations

import itertools
import json
import types

import click
import jsonschema
import pytest
from click.testing import CliRunner

from c2c import cli, errors, paths
from c2c.envelope import SCHEMA_PATH, Envelope
from conftest import c2c

SCHEMA = json.loads(SCHEMA_PATH.read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())

FAILED_CONTROL = {
    "id": "CTL-TIE-AR", "violating_rows": 2,
    "detail": {"row_hashes": ["ab12", "cd34"], "number_ids": ["ar/2026-03/total"]},
    "remediation": ["Re-run the load for 2026-03.", "Compare the invoice register with the AR ledger."],
}

# One error of each kind, in the order they win.
ORDER = [
    errors.LockHeld(),
    errors.UsageError("bad flag"),
    errors.PreconditionError("month not closed"),
    errors.ControlFailure("a control failed", [FAILED_CONTROL]),
    errors.CheckMismatch("differs from truth"),
    errors.ProvenanceError("digest mismatch"),
    errors.InternalError("dbt failed"),
]
EXITS = [70, 2, 4, 3, 5, 6, 70]


def valid(document: dict) -> None:
    VALIDATOR.validate(document)


def invalid(document: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(document)


# ------------------------------------------------------------------ the schema


def test_the_schema_is_itself_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_a_success_envelope_validates():
    envelope = Envelope(command="close", status="ok", exit=0, month="2026-03", result_hash="abc",
                        controls=[{"id": "CTL-TIE-AR", "status": "pass", "violating_rows": 0}])
    valid(envelope.to_dict())


def test_the_minimal_envelope_validates():
    valid(Envelope(command="status", status="ok", exit=0).to_dict())


def test_a_warning_envelope_has_exit_1():
    valid(Envelope(command="doctor", status="warning", exit=1, message="1 warning").to_dict())
    invalid({"schema_version": 1, "command": "doctor", "status": "warning", "exit": 0})


def test_extra_fields_are_carried_and_unknown_fields_are_allowed():
    alerts = [{"case": "downside", "first_breach_week": "W07", "shortfall_cents": 12345}]
    envelope = Envelope(command="forecast", status="ok", exit=0,
                        extra={"as_of": "2026-03-31", "cases": ["base", "downside", "upside"], "alerts": alerts})
    document = envelope.to_dict()
    assert document["alerts"] == alerts
    valid(document)
    valid({**document, "a_field_from_a_later_version": 1})


@pytest.mark.parametrize("error, code", list(zip(ORDER, EXITS)))
def test_every_error_class_maps_to_its_exit_and_validates(error, code):
    assert error.exit == code
    document = Envelope.from_error("close", error, month="2026-03").to_dict()
    assert document["exit"] == code and document["status"] == "failed"
    valid(document)


def test_failed_controls_carry_detail_and_remediation():
    document = Envelope.from_error("close", ORDER[3]).to_dict()
    assert document["failed_controls"] == [FAILED_CONTROL]
    invalid({**document, "failed_controls": [{k: v for k, v in FAILED_CONTROL.items() if k != "remediation"}]})
    invalid({**document, "failed_controls": [{**FAILED_CONTROL, "detail": {"row_hashes": []}}]})


@pytest.mark.parametrize("document", [
    {"command": "close", "status": "ok", "exit": 0},                                  # no schema_version
    {"schema_version": 2, "command": "close", "status": "ok", "exit": 0},
    {"schema_version": 1, "command": "close", "status": "ok", "exit": 2},             # ok must be 0
    {"schema_version": 1, "command": "close", "status": "failed", "exit": 0, "message": "x"},
    {"schema_version": 1, "command": "close", "status": "failed", "exit": 3},         # failed needs a message
    {"schema_version": 1, "command": "close", "status": "failed", "exit": 7, "message": "x"},
    {"schema_version": 1, "command": "close", "status": "ok", "exit": 0, "month": "2026-13"},
    {"schema_version": 1, "command": "close", "status": "ok", "exit": 0, "controls": [{"id": "CTL-X"}]},
])
def test_the_schema_rejects_a_malformed_envelope(document):
    invalid(document)


def test_render_writes_json_to_stdout_and_text_to_stderr(capsys):
    envelope = Envelope.from_error("close", ORDER[3], month="2026-03")
    envelope.render(json=True)
    out, err = capsys.readouterr()
    assert json.loads(out) == envelope.to_dict()
    assert "a control failed" in err and "CTL-TIE-AR: 2 violating row(s)" in err
    assert "Compare the invoice register with the AR ledger." in err

    envelope.render(json=False)
    out, err = capsys.readouterr()
    assert out == "" and "a control failed" in err


# ------------------------------------------------------------------ precedence


def test_precedence_is_lock_then_2_4_3_5_6_then_internal():
    for shuffled in itertools.permutations(ORDER):
        assert errors.precedence(shuffled) is ORDER[0]
    for i in range(len(ORDER)):
        assert errors.precedence(reversed(ORDER[i:])) is ORDER[i]


def test_precedence_of_the_exit_codes():
    assert [errors.precedence(e for e in ORDER[1:] if e.exit in codes).exit
            for codes in ({2, 3, 4, 5, 6}, {3, 4, 5, 6}, {3, 5, 6}, {5, 6}, {6})] == [2, 4, 3, 5, 6]


def test_an_unlisted_error_ranks_after_the_listed_ones_and_nothing_gives_none():
    stray = errors.C2CError(9, "stray")
    assert errors.precedence([stray, ORDER[5]]) is ORDER[5]
    assert errors.precedence([stray]) is stray
    assert errors.precedence([]) is None


def test_lock_held_message_and_internal_exit():
    assert errors.LockHeld().message == "another c2c process holds the warehouse"
    assert errors.InternalError("x").exit == 70


# ------------------------------------------------------------------ the group


def run_fake(monkeypatch, raises, *flags: str, lock_mode=None):
    """Invoke the real group with a stand-in command that raises `raises` (or returns)."""

    @click.command(name="fake")
    @paths.path_options
    def command() -> None:
        if raises is not None:
            raise raises
        click.echo("ran")

    monkeypatch.setattr(cli, "_module_names", lambda: ["fake"])
    monkeypatch.setattr(cli, "_load", lambda name: types.SimpleNamespace(command=command, LOCK=lock_mode))
    return CliRunner().invoke(cli.main, ["fake", *flags])


@pytest.mark.parametrize("error, code", list(zip(ORDER, EXITS)))
def test_the_group_exits_with_the_error_code_and_emits_the_envelope(monkeypatch, error, code):
    result = run_fake(monkeypatch, error, "--json")
    assert result.exit_code == code
    document = json.loads(result.stdout)
    valid(document)
    assert document["exit"] == code and document["command"] == "fake"
    assert document["message"] == error.message
    assert error.message in result.stderr


def test_without_json_only_text_is_written_and_stdout_stays_empty(monkeypatch):
    result = run_fake(monkeypatch, errors.PreconditionError("month not closed"))
    assert result.exit_code == 4
    assert result.stdout == ""
    assert "month not closed" in result.stderr


def test_a_failed_control_reaches_the_envelope(monkeypatch):
    result = run_fake(monkeypatch, ORDER[3], "--json")
    assert json.loads(result.stdout)["failed_controls"] == [FAILED_CONTROL]


def test_an_unexpected_exception_is_70_with_the_last_traceback_line(monkeypatch):
    result = run_fake(monkeypatch, ValueError("boom"), "--json")
    assert result.exit_code == 70
    document = json.loads(result.stdout)
    valid(document)
    assert document["message"] == "ValueError: boom"
    assert "Traceback" in result.stderr


def test_a_click_usage_error_is_2_even_before_the_command_parses(monkeypatch):
    result = run_fake(monkeypatch, None, "--json", "--no-such-flag")
    assert result.exit_code == 2
    document = json.loads(result.stdout)
    valid(document)
    assert "no-such-flag" in document["message"]


def test_an_unknown_command_is_2():
    result = CliRunner().invoke(cli.main, ["no-such-command", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["exit"] == 2


def test_help_and_a_clean_run_are_untouched(monkeypatch):
    assert CliRunner().invoke(cli.main, ["--help"]).exit_code == 0
    result = run_fake(monkeypatch, None, "--json")
    assert result.exit_code == 0 and result.stdout == "ran\n"


def test_the_lock_is_taken_before_the_command_runs(monkeypatch, tmp_path):
    warehouse = tmp_path / "w.duckdb"
    result = run_fake(monkeypatch, None, "--warehouse", str(warehouse), lock_mode="exclusive")
    assert result.exit_code == 0
    assert (tmp_path / "w.duckdb.lock").exists()


# ------------------------------------------------------------------ the shipped commands


def test_gen_emits_a_valid_envelope(tmp_path):
    result = c2c("gen", "--fixture", "small", "--out", str(tmp_path), "--json")
    assert result.code == 0, result.stderr
    document = result.json
    valid(document)
    assert document["command"] == "gen" and document["status"] == "ok"


def test_gen_failures_emit_valid_envelopes(tmp_path):
    missing = c2c("gen", "--fixture", "small", "--out", str(tmp_path), "--verify", "--json")
    assert missing.code == 4
    valid(missing.json)

    usage = c2c("gen", "--json")
    assert usage.code == 2
    valid(usage.json)
    assert usage.json["message"].startswith("name a fixture")

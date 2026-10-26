import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
from pydantic import SecretStr
from sqlalchemy import CheckConstraint, MetaData, create_engine, select
from sqlalchemy.orm import Session

from portfolio_digest import cli
from portfolio_digest.akahu import AkahuClient, AkahuError, exact_units, push_success
from portfolio_digest.akahu_sync import sync
from portfolio_digest.models import (
    AkahuMapping,
    AkahuSnapshot,
    Base,
    Income,
    Instrument,
    Portfolio,
    Trade,
)
from portfolio_digest.reconcile import ledger_units, reconcile

APP = "made-up-app-secret"
USER = "made-up-user-secret"


def response(items, cursor=None):
    return BytesIO(
        json.dumps({"success": True, "items": items, "cursor": {"next": cursor}}).encode()
    )


def client_with(*responses):
    opener = Mock(side_effect=responses)
    sleep = Mock()
    return (
        AkahuClient(SecretStr(APP), SecretStr(USER), open_request=opener, sleep=sleep),
        opener,
        sleep,
    )


def test_pagination_and_decimal_json_without_float_conversion():
    client, opener, _ = client_with(
        response([{"_id": "acc_fictional"}], "next & page"),
        response([{"_id": "acc_other"}]),
        BytesIO(b'{"success":true,"items":[{"units":0.123456789123456789}]}'),
    )
    assert len(client.accounts()) == 2
    assert opener.call_args_list[1].args[0].full_url.endswith("cursor=next+%26+page")
    assert client.holdings("acc_fictional")[0]["units"] == Decimal("0.123456789123456789")
    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.get_header("X-akahu-id") == APP
    assert request.get_header("Authorization") == "Bearer " + USER


def test_holdings_pagination_and_429_retry():
    client, opener, sleep = client_with(
        HTTPError("https://api.akahu.io", 429, USER, {"Retry-After": "3"}, BytesIO(APP.encode())),
        response([{"_id": "h1"}], "page2"),
        response([{"_id": "h2"}]),
    )
    assert len(client.holdings("acc_fictional")) == 2
    sleep.assert_called_once_with(3)
    assert opener.call_count == 3


def test_rate_limits_are_bounded_and_back_off():
    client, opener, sleep = client_with(
        *[HTTPError("https://api.akahu.io", 429, USER, {}, BytesIO(APP.encode())) for _ in range(5)]
    )
    with pytest.raises(AkahuError, match="HTTP 429"):
        client.accounts()
    assert opener.call_count == 5
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2, 4, 8]


@pytest.mark.parametrize(
    "error",
    [
        HTTPError("https://api.akahu.io", 401, USER, {}, BytesIO(APP.encode())),
        URLError(APP + USER),
        BytesIO((APP + USER).encode()),
        BytesIO(json.dumps({"success": False, "message": APP + USER}).encode()),
    ],
)
def test_upstream_secrets_never_appear_in_errors_or_logs(error, caplog, capsys):
    client, _, _ = client_with(error)
    with pytest.raises(AkahuError) as exc:
        client.accounts()
    output = str(exc.value) + repr(client) + caplog.text + str(capsys.readouterr())
    assert APP not in output and USER not in output


def test_repeated_cursor_fails_instead_of_looping():
    client, _, _ = client_with(response([], "again"), response([], "again"))
    with pytest.raises(AkahuError, match="pagination"):
        client.accounts()


@pytest.mark.parametrize("value", [True, 0.1, "NaN", "Infinity", "bad", None])
def test_invalid_units_are_safe_errors(value):
    with pytest.raises(AkahuError):
        exact_units(value)


@pytest.fixture
def local_engine():
    # Exercise persistence without a server. Postgres-specific CHECKs are covered by
    # test_schema against Postgres; SQLite cannot parse those expressions.
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copy = table.to_metadata(metadata)
        for constraint in list(copy.constraints):
            if isinstance(constraint, CheckConstraint):
                copy.constraints.remove(constraint)
    metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Portfolio(id=1, name="Fictional"))
        session.add_all(
            [
                Instrument(id=1, exchange="NZX", code="DEMO", name="Example", currency="NZD"),
                Instrument(id=2, exchange="ASX", code="OTHER", name="Other", currency="AUD"),
            ]
        )
        session.add(
            Trade(
                portfolio_id=1,
                instrument_id=1,
                trade_date=date(2026, 1, 1),
                trade_type="buy",
                quantity=Decimal("10.25"),
                price=Decimal("1"),
                currency="NZD",
                source="manual",
                external_ref="fake1",
            )
        )
        session.commit()
    yield engine
    engine.dispose()


def sync_client(holdings):
    return client_with(
        response(
            [
                {
                    "_id": "acc_fictional",
                    "type": "INVESTMENT",
                    "status": "ACTIVE",
                    "balance": {"current": "10.25", "currency": "NZD"},
                    "refreshed": {"balance": "2026-10-25T12:00:00Z"},
                }
            ]
        ),
        response(holdings),
    )[0]


def holding(units="10.25", **kwargs):
    return {"_id": "hold_fictional", "code": "DEMO", "exchange": "NZX", "units": units, **kwargs}


@pytest.mark.parametrize(
    ("rows", "expected_differences", "unmapped"),
    [
        ([holding()], [], 0),
        ([holding("11.25")], [Decimal("1")], 0),
        ([holding("9.25")], [Decimal("-1")], 0),
        ([], [Decimal("-10.25")], 0),
        (
            [holding(), holding("2", _id="hold_other", code="OTHER", exchange="ASX")],
            [Decimal("2")],
            0,
        ),
        ([holding(code="UNKNOWN")], [Decimal("-10.25")], 1),
    ],
)
def test_sync_persists_match_drift_both_ways_and_unmapped(
    local_engine, rows, expected_differences, unmapped
):
    snapshot_id, mismatches, count = sync(local_engine, "fictional", sync_client(rows), [])
    assert [m.difference for m in mismatches] == expected_differences
    assert count == unmapped
    with Session(local_engine) as session:
        snapshot = session.get(AkahuSnapshot, snapshot_id)
        assert snapshot.status == ("drift" if expected_differences or unmapped else "matched")
        assert snapshot.finished_at is not None
        assert (
            json.loads(snapshot.observations)[0]["refreshed"]["balance"] == "2026-10-25T12:00:00Z"
        )
        assert session.scalars(select(Trade)).one().quantity == Decimal("10.25")


def test_ambiguous_code_needs_manual_account_scoped_mapping(local_engine):
    with Session(local_engine) as session:
        session.add(Instrument(id=3, exchange="ASX", code="DEMO", name="Third", currency="AUD"))
        session.commit()
    row = holding()
    row.pop("exchange")
    assert sync(local_engine, "Fictional", sync_client([row]), [])[2] == 1
    with Session(local_engine) as session:
        session.add(
            AkahuMapping(
                portfolio_id=1,
                account_id="acc_fictional",
                holding_id="hold_fictional",
                instrument_id=1,
            )
        )
        session.commit()
    assert sync(local_engine, "Fictional", sync_client([row]), [])[1:] == ([], 0)


def test_failed_fetch_leaves_failed_snapshot(local_engine):
    client, _, _ = client_with(URLError(USER))
    with pytest.raises(AkahuError):
        sync(local_engine, "Fictional", client, [])
    with Session(local_engine) as session:
        snapshot = session.scalars(select(AkahuSnapshot)).one()
        assert snapshot.status == "failed"
        assert snapshot.finished_at is not None
        assert USER not in snapshot.report


def test_ledger_sells_adjustments_opening_balance_and_drp(local_engine):
    with Session(local_engine) as session:
        for index, (kind, quantity) in enumerate(
            [("sell", "3"), ("adjustment", "-1"), ("opening_balance", "2")]
        ):
            session.add(
                Trade(
                    portfolio_id=1,
                    instrument_id=1,
                    trade_date=date(2026, 1, 2),
                    trade_type=kind,
                    quantity=Decimal(quantity),
                    price=Decimal("0"),
                    currency="NZD",
                    source="manual",
                    external_ref=f"fake{index + 2}",
                )
            )
        session.add(
            Income(
                portfolio_id=1,
                instrument_id=1,
                paid_date=date(2026, 1, 3),
                kind="dividend",
                currency="NZD",
                gross_amount=Decimal("1"),
                net_amount=Decimal("1"),
                source="manual",
                external_ref="fake-income",
                drp_quantity=Decimal("0.5"),
            )
        )
        session.commit()
        assert ledger_units(session, 1) == {1: Decimal("8.75")}
    assert reconcile({1: Decimal("8.75")}, {1: Decimal("8.75")}) == []


@pytest.mark.parametrize(("rows", "exit_code"), [([holding()], 0), ([holding("11")], 1)])
def test_cli_exit_and_kuma_only_on_match(local_engine, monkeypatch, capsys, rows, exit_code):
    settings = Mock(
        akahu_app_token=SecretStr(APP),
        akahu_user_token=SecretStr(USER),
        akahu_account_ids=[],
        uptime_kuma_push_url=SecretStr("https://kuma.invalid/secret"),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "make_engine", lambda: local_engine)
    monkeypatch.setattr(cli, "AkahuClient", lambda *args: sync_client(rows))
    push = Mock()
    monkeypatch.setattr(cli, "push_success", push)
    assert cli.main(["akahu", "sync", "--portfolio", "Fictional"]) == exit_code
    assert push.call_count == (1 if exit_code == 0 else 0)
    output = str(capsys.readouterr())
    assert APP not in output and USER not in output


def test_kuma_failure_does_not_leak_url(monkeypatch):
    opener = Mock()
    opener.open.side_effect = URLError("https://kuma.invalid/" + USER)
    monkeypatch.setattr("portfolio_digest.akahu.build_opener", lambda *args: opener)
    with pytest.raises(AkahuError) as exc:
        push_success(SecretStr("https://kuma.invalid/" + USER))
    assert USER not in str(exc.value)


def test_unsupported_unit_precision_fails_instead_of_disappearing():
    with pytest.raises(AkahuError):
        exact_units("1e-60")
    assert exact_units("0.000000000000000000000001") == Decimal("1e-24")


def test_non_standard_json_numbers_are_rejected():
    client, _, _ = client_with(BytesIO(b'{"success":true,"items":[{"balance":NaN}]}'))
    with pytest.raises(AkahuError):
        client.accounts()


def test_malformed_retry_after_uses_backoff():
    client, _, sleep = client_with(
        HTTPError("https://api.akahu.io", 429, USER, {"Retry-After": "²"}, BytesIO()),
        response([]),
    )
    assert client.accounts() == []
    sleep.assert_called_once_with(1)


def test_http_protocol_errors_do_not_expose_secrets():
    from http.client import BadStatusLine

    client, _, _ = client_with(BadStatusLine(APP + USER))
    with pytest.raises(AkahuError) as exc:
        client.accounts()
    assert APP not in str(exc.value) and USER not in str(exc.value)


@pytest.mark.parametrize("suffix", ["\x00", "\n", "#fragment"])
def test_malformed_kuma_url_never_exposes_secret(suffix, monkeypatch):
    from http.client import InvalidURL

    opener = Mock()
    opener.open.side_effect = InvalidURL(USER)
    monkeypatch.setattr("portfolio_digest.akahu.build_opener", lambda *args: opener)
    with pytest.raises(AkahuError) as exc:
        push_success(SecretStr("http://localhost/" + USER + suffix))
    assert USER not in str(exc.value)


def test_duplicate_holdings_fail_and_do_not_save_partial_match(local_engine):
    with pytest.raises(AkahuError, match="duplicate holding"):
        sync(local_engine, "Fictional", sync_client([holding(), holding()]), [])
    with Session(local_engine) as session:
        assert session.scalars(select(AkahuSnapshot)).one().status == "failed"


def test_missing_account_fails_instead_of_confirming_zero(local_engine):
    with pytest.raises(AkahuError, match="missing"):
        sync(local_engine, "Fictional", sync_client([]), ["acc_absent"])


def test_no_investment_accounts_fails(local_engine):
    client, _, _ = client_with(response([{"_id": "acc_bank", "type": "CHECKING"}]))
    with pytest.raises(AkahuError, match="No investment accounts"):
        sync(local_engine, "Fictional", client, [])


def test_multiple_accounts_aggregate_units(local_engine):
    client, _, _ = client_with(
        response(
            [
                {"_id": "acc_one", "type": "INVESTMENT", "status": "ACTIVE"},
                {"_id": "acc_two", "type": "INVESTMENT", "status": "ACTIVE"},
            ]
        ),
        response([holding("5")]),
        response([holding("5.25")]),
    )
    assert sync(local_engine, "Fictional", client, [])[1:] == ([], 0)


def test_bad_account_json_is_a_safe_configuration_error(no_settings_file, monkeypatch, capsys):
    from portfolio_digest.config import get_settings

    monkeypatch.setenv("AKAHU_ACCOUNT_IDS", APP)
    get_settings.cache_clear()
    assert cli.main(["akahu", "sync", "--portfolio", "Fictional"]) == 1
    assert APP not in capsys.readouterr().err


def test_redirects_are_not_followed():
    from portfolio_digest.akahu import NoRedirect

    assert NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


def test_inactive_investment_account_fails(local_engine):
    client, _, _ = client_with(
        response([{"_id": "acc_fictional", "type": "INVESTMENT", "status": "INACTIVE"}])
    )
    with pytest.raises(AkahuError, match="inactive"):
        sync(local_engine, "Fictional", client, [])


@pytest.mark.parametrize("status_query", ["", "&status=up", "&status=down&status=down"])
def test_kuma_success_push_is_read_only_and_preserves_query(monkeypatch, status_query):
    opener = Mock()
    opener.open.return_value = BytesIO(b'{"ok":true}')
    monkeypatch.setattr("portfolio_digest.akahu.build_opener", lambda *args: opener)
    push_success(SecretStr("https://kuma.invalid/push/fake?msg=OK&ping=" + status_query))
    opener.open.assert_called_once_with(
        "https://kuma.invalid/push/fake?msg=OK&ping=&status=up", timeout=15
    )

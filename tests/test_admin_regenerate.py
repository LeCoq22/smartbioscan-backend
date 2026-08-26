from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from api.admin_regenerate import RegenerateBatchRequest, _get_reports_for_batch


def test_batch_can_select_only_explicit_report_ids():
    db = MagicMock()
    query = db.client.table.return_value.select.return_value
    query.in_.return_value = query
    query.eq.return_value = query
    query.order.return_value = query
    query.limit.return_value = query
    query.execute.return_value.data = [{"id": "report-2"}]

    body = RegenerateBatchRequest(
        dry_run=False,
        nutri_id="nutri-1",
        report_ids=["report-2", "report-2", "report-3"],
    )
    rows = _get_reports_for_batch(db, body)

    query.in_.assert_called_once_with("id", ["report-2", "report-3"])
    query.eq.assert_called_once_with("nutri_id", "nutri-1")
    assert rows == [{"id": "report-2"}]


def test_empty_report_id_selection_regenerates_nothing():
    db = MagicMock()
    body = RegenerateBatchRequest(report_ids=[])

    assert _get_reports_for_batch(db, body) == []
    db.client.table.assert_not_called()


def test_batch_rejects_more_than_2000_report_ids():
    with pytest.raises(ValidationError, match="máximo 2000"):
        RegenerateBatchRequest(report_ids=[f"report-{i}" for i in range(2001)])

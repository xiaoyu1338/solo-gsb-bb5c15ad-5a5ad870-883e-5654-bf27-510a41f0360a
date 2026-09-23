"""POST /api/v1/inventory/allocate 的 HTTP API 测试（试剂批次 FEFO 分配）。

覆盖：失效日期优先 / 同日批号升序、纯度换算、过期与隔离批次跳过、
跨批次拆分、逐批余量与未满足需求、汇总状态、批号重复拒绝（防止重复
扣减）、请求间无状态以及各类非法输入的 4xx 响应。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PATH = "/api/v1/inventory/allocate"

AS_OF = "2026-09-23"


def _batch(batch_id, purity="1", moles="1", expiry="2026-12-31", status="available"):
    return {
        "batch_id": batch_id,
        "purity": purity,
        "available_moles": moles,
        "expiry_date": expiry,
        "status": status,
    }


def _post(species="NaCl", required="1", as_of=AS_OF, batches=()):
    return client.post(
        PATH,
        json={
            "species": species,
            "required_moles": required,
            "as_of_date": as_of,
            "batches": list(batches),
        },
    )


# ------------------------------------------------------------ 成功路径
def test_fefo_order_split_and_purity_conversion():
    # FEFO：B001/B002 同日（批号升序 B001 先），B003 最后；
    # 有效量 = 名义量 × 纯度，需求 1.5 mol 跨 B001、B002 两批拆分
    resp = _post(
        required="1.5",
        batches=[
            _batch("B003", purity="1", moles="1", expiry="2026-12-01"),
            _batch("B002", purity="0.5", moles="3", expiry="2026-10-01"),
            _batch("B001", purity="0.8", moles="1", expiry="2026-10-01"),
        ],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "fulfilled"
    assert data["allocated_moles"] == "1.5"
    assert data["unmet_moles"] == "0"
    assert data["skipped_batches"] == []

    # 分配顺序：失效日期升序，同日按批号升序（与请求顺序无关）
    lines = data["allocations"]
    assert [line["batch_id"] for line in lines] == ["B001", "B002", "B003"]

    b001 = lines[0]
    assert b001["allocated_effective_moles"] == "0.8"  # 1 × 0.8
    assert b001["allocated_nominal_moles"] == "1"
    assert b001["remaining_nominal_moles"] == "0"
    assert b001["remaining_effective_moles"] == "0"
    assert b001["purity"] == "0.8"
    assert b001["expiry_date"] == "2026-10-01"

    b002 = lines[1]
    assert b002["allocated_effective_moles"] == "0.7"  # 剩余需求
    assert b002["allocated_nominal_moles"] == "1.4"  # 0.7 ÷ 0.5
    assert b002["remaining_nominal_moles"] == "1.6"
    assert b002["remaining_effective_moles"] == "0.8"

    # 需求已满足，B003 未被动用但仍在明细中（领用 0、余量完整）
    b003 = lines[2]
    assert b003["allocated_effective_moles"] == "0"
    assert b003["allocated_nominal_moles"] == "0"
    assert b003["remaining_nominal_moles"] == "1"
    assert b003["remaining_effective_moles"] == "1"


def test_expired_and_quarantined_batches_are_skipped():
    resp = _post(
        species="KCl",
        required="2",
        batches=[
            _batch("OLD", moles="10", expiry="2026-09-22"),  # 已过期
            _batch("HOLD", moles="10", expiry="2027-01-01", status="quarantined"),
            _batch("OK", purity="0.5", moles="1", expiry=AS_OF),  # 当天到期仍可用
        ],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "partial"
    assert data["allocated_moles"] == "0.5"
    assert data["unmet_moles"] == "1.5"
    assert [line["batch_id"] for line in data["allocations"]] == ["OK"]
    assert data["skipped_batches"] == [
        {"batch_id": "OLD", "reason": "expired"},
        {"batch_id": "HOLD", "reason": "quarantined"},
    ]


def test_quarantined_and_expired_reports_quarantined():
    resp = _post(
        required="1",
        batches=[_batch("BOTH", expiry="2020-01-01", status="quarantined")],
    )
    data = resp.json()
    assert data["skipped_batches"] == [{"batch_id": "BOTH", "reason": "quarantined"}]
    assert data["status"] == "unavailable"


def test_unavailable_when_nothing_eligible():
    resp = _post(required="2", batches=[])
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unavailable"
    assert data["allocated_moles"] == "0"
    assert data["unmet_moles"] == "2"
    assert data["allocations"] == []
    assert data["skipped_batches"] == []


def test_single_batch_never_overdrawn():
    # 需求超过单批可用量：领用 capped 在可用量，余量归零，剩余需求记为未满足
    resp = _post(required="5", batches=[_batch("ONLY", moles="2")])
    data = resp.json()
    assert data["status"] == "partial"
    assert data["allocated_moles"] == "2"
    assert data["unmet_moles"] == "3"
    line = data["allocations"][0]
    assert line["allocated_nominal_moles"] == "2"
    assert line["remaining_nominal_moles"] == "0"
    assert line["remaining_effective_moles"] == "0"


def test_zero_availability_batch_contributes_nothing():
    resp = _post(required="1", batches=[_batch("EMPTY", moles="0")])
    data = resp.json()
    assert data["status"] == "unavailable"
    line = data["allocations"][0]
    assert line["allocated_effective_moles"] == "0"
    assert line["remaining_nominal_moles"] == "0"


def test_exact_decimal_math_without_float_artifacts():
    # 0.1 / 0.3 = 1/3：名义扣减与余量按 28 位有效数字精确渲染
    resp = _post(
        required="0.1",
        batches=[_batch("FRAC", purity="0.3", moles="1")],
    )
    data = resp.json()
    line = data["allocations"][0]
    assert line["allocated_effective_moles"] == "0.1"
    assert line["allocated_nominal_moles"] == "0.3333333333333333333333333333"
    assert line["remaining_nominal_moles"] == "0.6666666666666666666666666667"
    assert line["remaining_effective_moles"] == "0.2"
    assert data["status"] == "fulfilled"


def test_species_is_normalized_and_echoed():
    resp = _post(species=" Ca[OH]2 ", batches=[])
    assert resp.status_code == 200
    assert resp.json()["species"] == {"input": "Ca[OH]2", "normalized": "Ca(OH)2"}


def test_request_is_stateless_between_calls():
    # 同一请求提交两次结果完全一致：服务不在请求间保留扣减状态
    body = {
        "species": "NaCl",
        "required_moles": "1.5",
        "as_of_date": AS_OF,
        "batches": [_batch("B1", moles="1"), _batch("B2", moles="1")],
    }
    first = client.post(PATH, json=body).json()
    second = client.post(PATH, json=body).json()
    assert first == second
    assert first["allocated_moles"] == "1.5"


def test_response_shape_regression_guard():
    resp = _post(
        required="1",
        batches=[
            _batch("B1", moles="2"),
            _batch("OLD", expiry="2020-01-01"),
        ],
    )
    data = resp.json()
    assert set(data) == {
        "species", "required_moles", "as_of_date", "status",
        "allocated_moles", "unmet_moles", "allocations", "skipped_batches",
    }
    assert set(data["species"]) == {"input", "normalized"}
    assert set(data["allocations"][0]) == {
        "batch_id", "purity", "expiry_date",
        "allocated_effective_moles", "allocated_nominal_moles",
        "remaining_nominal_moles", "remaining_effective_moles",
    }
    assert set(data["skipped_batches"][0]) == {"batch_id", "reason"}
    assert data["as_of_date"] == AS_OF
    assert isinstance(data["allocations"][0]["allocated_effective_moles"], str)


# ------------------------------------------------------------ 结构化错误
def test_species_parse_error_is_400():
    resp = _post(species="Zz")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "FORMULA_PARSE_ERROR"
    assert body["error"]["details"]["field"] == "species"
    assert body["error"]["details"]["input"] == "Zz"


@pytest.mark.parametrize("bad", ["0", "-0.0", "-1", "abc", "NaN", "Infinity", "1e10001"])
def test_invalid_required_moles_is_422(bad):
    resp = _post(required=bad)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["invalid_required_moles"] == bad


def test_required_moles_must_be_string_not_json_number():
    resp = client.post(
        PATH,
        json={
            "species": "NaCl",
            "required_moles": 1.5,
            "as_of_date": AS_OF,
            "batches": [],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_duplicate_batch_ids_rejected():
    resp = _post(
        required="1",
        batches=[_batch("B1"), _batch("B1", moles="2"), _batch("B2")],
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["duplicate_batch_ids"] == ["B1"]


@pytest.mark.parametrize("bad", ["0", "-0.5", "1.0001", "2", "abc", "NaN"])
def test_invalid_purity_is_422(bad):
    resp = _post(required="1", batches=[_batch("B1", purity=bad)])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["invalid_batches"] == [
        {"batch_id": "B1", "field": "purity", "value": bad}
    ]


@pytest.mark.parametrize("bad", ["-1", "-0.0", "abc", "Infinity"])
def test_invalid_available_moles_is_422(bad):
    resp = _post(required="1", batches=[_batch("B1", moles=bad)])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["invalid_batches"] == [
        {"batch_id": "B1", "field": "available_moles", "value": bad}
    ]


def test_multiple_invalid_batches_are_aggregated():
    resp = _post(
        required="1",
        batches=[
            _batch("B1", purity="0"),
            _batch("B2", moles="-3"),
            _batch("B3"),
        ],
    )
    assert resp.status_code == 422
    invalid = resp.json()["error"]["details"]["invalid_batches"]
    assert invalid == [
        {"batch_id": "B1", "field": "purity", "value": "0"},
        {"batch_id": "B2", "field": "available_moles", "value": "-3"},
    ]


def test_purity_must_be_string_not_json_number():
    resp = client.post(
        PATH,
        json={
            "species": "NaCl",
            "required_moles": "1",
            "as_of_date": AS_OF,
            "batches": [
                {
                    "batch_id": "B1",
                    "purity": 0.5,
                    "available_moles": "1",
                    "expiry_date": "2026-12-31",
                    "status": "available",
                }
            ],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "overrides",
    [
        {"expiry_date": "2026-13-01"},   # 不存在的月份
        {"expiry_date": "not-a-date"},
        {"expiry_date": "2026/12/31"},
        {"status": "held"},              # 非法状态
        {"status": ""},
        {"batch_id": "   "},             # 空白批号
    ],
)
def test_invalid_batch_fields_are_422(overrides):
    batch = _batch("B1")
    batch.update(overrides)
    resp = _post(required="1", batches=[batch])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("bad_date", ["2026-02-30", "23/09/2026", "tomorrow"])
def test_invalid_as_of_date_is_422(bad_date):
    resp = _post(required="1", as_of=bad_date)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "payload",
    [
        # 缺字段
        {"required_moles": "1", "as_of_date": AS_OF, "batches": []},
        {"species": "NaCl", "as_of_date": AS_OF, "batches": []},
        {"species": "NaCl", "required_moles": "1", "batches": []},
        {"species": "NaCl", "required_moles": "1", "as_of_date": AS_OF},
        # 空白物种 / 需求
        {"species": "  ", "required_moles": "1", "as_of_date": AS_OF, "batches": []},
        {"species": "NaCl", "required_moles": " ", "as_of_date": AS_OF, "batches": []},
        # 批次缺字段
        {"species": "NaCl", "required_moles": "1", "as_of_date": AS_OF,
         "batches": [{"batch_id": "B1"}]},
    ],
)
def test_request_shape_validation(payload):
    resp = client.post(PATH, json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"

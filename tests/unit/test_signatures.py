import hashlib

from coreman.core.auth.signatures import canonical_params, sign_request


def test_php_compatible_scalars_array_and_nested_skip() -> None:
    params = {
        "z": "a b~",
        "ids": [1, None, True, False],
        "a": "中文",
        "nested": [{"x": 1}],
        "object": {"x": 1},
    }
    assert (
        canonical_params(params)
        == "a=%E4%B8%AD%E6%96%87&ids[0]=1&ids[1]=&ids[2]=1&ids[3]=&z=a+b%7E"
    )


def test_actual_alias_path_is_part_of_signature() -> None:
    raw = "POSTapi/robot/examplea=1" + "1000" + "client" + "test-secret"
    assert (
        sign_request("POST", "/api/robot/example", {"a": 1}, "1000", "client", "test-secret")
        == hashlib.sha256(raw.encode()).hexdigest()
    )
    assert sign_request("GET", "/api/infra/x", {}, "1000", "c", "s") != sign_request(
        "GET", "/api/robot/x", {}, "1000", "c", "s"
    )

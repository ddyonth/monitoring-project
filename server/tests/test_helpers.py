"""
Юнит-тесты чистых вспомогательных функций server_app.

Границы и форматы взяты из реализации, а не из описания задачи:
- _severity_from_ratio: None -> "med"; ratio > 10 -> "high"; ratio >= 5 -> "med"; иначе "low".
- _raise_severity: неизвестное значение (и current, и target) получает ранг 1 (как "med").
- _binary_key: sha256 только strip (регистр сохраняется); exe_path strip+lower с префиксом "path:";
  process_name strip+lower с префиксом "name:", пустое имя -> "name:unknown".
- _normalize_allowed_process_types: None -> []; list -> элементы как есть;
  любое другое значение -> str(raw).split(","). JSON-строка НЕ парсится как JSON.
"""

import pytest
import server_app
from server_app import (
    _binary_key,
    _make_alert_dedup_key,
    _median,
    _normalize_allowed_process_types,
    _raise_severity,
    _severity_from_ratio,
)

# ---------------------------------------------------------------- _median

class TestMedian:
    def test_odd_count(self):
        assert _median([3, 1, 2]) == 2.0

    def test_even_count_is_mean_of_two_middle(self):
        assert _median([4, 1, 3, 2]) == 2.5

    def test_empty_returns_none(self):
        assert _median([]) is None

    def test_none_values_are_skipped(self):
        assert _median([None, 5, None, 1, 3]) == 3.0

    def test_only_none_returns_none(self):
        assert _median([None, None]) is None

    def test_result_is_float(self):
        assert isinstance(_median([1, 2, 3]), float)


# ------------------------------------------------------ _severity_from_ratio

class TestSeverityFromRatio:
    def test_none_is_med(self):
        assert _severity_from_ratio(None) == "med"

    @pytest.mark.parametrize("ratio,expected", [
        (0.0, "low"),
        (4.99, "low"),
        (5.0, "med"),       # >= 5 -> med
        (7.5, "med"),
        (10.0, "med"),      # ровно 10 ещё med, потому что проверка строгая ">"
        (10.0001, "high"),
        (100.0, "high"),
    ])
    def test_boundaries(self, ratio, expected):
        assert _severity_from_ratio(ratio) == expected


# ------------------------------------------------------------ _raise_severity

class TestRaiseSeverity:
    @pytest.mark.parametrize("current,target,expected", [
        ("low", "low", "low"),
        ("low", "med", "med"),
        ("low", "high", "high"),
        ("med", "low", "med"),
        ("med", "med", "med"),
        ("med", "high", "high"),
        ("high", "low", "high"),
        ("high", "med", "high"),
        ("high", "high", "high"),
    ])
    def test_all_known_pairs(self, current, target, expected):
        assert _raise_severity(current, target) == expected

    def test_unknown_target_ranks_as_med(self):
        # order.get(unknown, 1) == 1: выше "low", не выше "med"/"high".
        # При current="low" функция возвращает сам неизвестный target — это
        # фактическое поведение кода, фиксируем его.
        assert _raise_severity("low", "critical") == "critical"
        assert _raise_severity("med", "critical") == "med"
        assert _raise_severity("high", "critical") == "high"


# ---------------------------------------------------------------- _binary_key

class TestBinaryKey:
    def test_sha256_has_priority(self):
        assert _binary_key("ABC", "C:\\x.exe", "x.exe") == "ABC"

    def test_sha256_is_stripped_but_case_preserved(self):
        assert _binary_key("  AbC  ", None, "x.exe") == "AbC"

    def test_empty_sha_falls_back_to_exe_path(self):
        assert _binary_key("", "C:\\Tools\\App.EXE", "app.exe") == "path:c:\\tools\\app.exe"

    def test_whitespace_only_sha_falls_back_to_exe_path(self):
        assert _binary_key("   ", "/usr/bin/app", "app") == "path:/usr/bin/app"

    def test_exe_path_is_stripped_and_lowercased(self):
        assert _binary_key(None, "  C:\\A\\B.exe  ", "b.exe") == "path:c:\\a\\b.exe"

    def test_no_sha_no_path_uses_process_name_lowercased(self):
        assert _binary_key(None, None, "  Notepad.EXE ") == "name:notepad.exe"

    def test_everything_empty_gives_name_unknown(self):
        assert _binary_key(None, "", "") == "name:unknown"
        assert _binary_key(None, None, None) == "name:unknown"


# ------------------------------------------------------ _make_alert_dedup_key

class TestMakeAlertDedupKey:
    def test_process_chain_branch_uses_chain_key(self):
        alert = {
            "entity_type": "process_chain",
            "machine_name": "PC1",
            "user_name": "alice",
            "chain_key": "parentsha -> childsha",
            "metric": "chain_rarity",
            "bucket_hour": "2026-09-15T09",
            # эти поля в ветке process_chain НЕ участвуют:
            "sha256": "should-not-appear",
            "exe_path": "C:\\nope.exe",
            "process_name": "nope.exe",
        }
        assert _make_alert_dedup_key(alert) == "PC1|alice|parentsha -> childsha|chain_rarity|2026-09-15T09"

    def test_process_chain_branch_with_missing_chain_key(self):
        alert = {
            "entity_type": "process_chain",
            "machine_name": "PC1",
            "user_name": "alice",
            "metric": "chain_rarity",
            "bucket_hour": "2026-09-15T09",
        }
        assert _make_alert_dedup_key(alert) == "PC1|alice||chain_rarity|2026-09-15T09"

    def test_process_session_branch_uses_binary_key_from_sha(self):
        alert = {
            "entity_type": "process_session",
            "machine_name": "PC1",
            "user_name": "alice",
            "sha256": "abc123",
            "exe_path": "C:\\x.exe",
            "process_name": "x.exe",
            "metric": "rss",
            "bucket_hour": "2026-09-15T09",
            "chain_key": "ignored-in-this-branch",
        }
        assert _make_alert_dedup_key(alert) == "PC1|alice|abc123|rss|2026-09-15T09"

    def test_process_session_branch_falls_back_to_exe_path(self):
        alert = {
            "entity_type": "process_session",
            "machine_name": "PC1",
            "user_name": "alice",
            "sha256": None,
            "exe_path": "C:\\Tools\\X.exe",
            "process_name": "x.exe",
            "metric": "rss",
            "bucket_hour": "2026-09-15T09",
        }
        assert _make_alert_dedup_key(alert) == "PC1|alice|path:c:\\tools\\x.exe|rss|2026-09-15T09"

    def test_process_session_branch_without_sha_and_path(self):
        alert = {
            "entity_type": "process_session",
            "machine_name": "PC1",
            "user_name": "alice",
            "metric": "rarity",
            "bucket_hour": "2026-09-15T09",
        }
        # process_name отсутствует -> alert.get(...) or '' -> "name:unknown"
        assert _make_alert_dedup_key(alert) == "PC1|alice|name:unknown|rarity|2026-09-15T09"


# ------------------------------------------- _normalize_allowed_process_types

class TestNormalizeAllowedProcessTypes:
    def test_none_gives_empty_list(self):
        assert _normalize_allowed_process_types(None) == []

    def test_empty_string_gives_empty_list(self):
        assert _normalize_allowed_process_types("") == []

    def test_comma_separated_string(self):
        assert _normalize_allowed_process_types("office, browser ,dev") == ["office", "browser", "dev"]

    def test_string_dedup_is_case_insensitive_and_keeps_first_spelling(self):
        assert _normalize_allowed_process_types("Office,office,OFFICE,browser") == ["Office", "browser"]

    def test_string_skips_empty_items(self):
        assert _normalize_allowed_process_types(",office,, ,browser,") == ["office", "browser"]

    def test_list_of_strings(self):
        assert _normalize_allowed_process_types([" office ", "browser"]) == ["office", "browser"]

    def test_list_dedup_and_none_items(self):
        assert _normalize_allowed_process_types(["office", None, "", "Office", "dev"]) == ["office", "dev"]

    def test_list_non_string_items_are_stringified(self):
        assert _normalize_allowed_process_types([1, 2, 1]) == ["1", "2"]

    def test_list_items_are_not_split_by_comma(self):
        # элементы списка берутся как есть, split(",") применяется только к строке
        assert _normalize_allowed_process_types(["office,browser"]) == ["office,browser"]

    def test_json_string_is_not_parsed_as_json(self):
        # Функция не знает про JSON: строка просто режется по запятой.
        assert _normalize_allowed_process_types('["office","browser"]') == ['["office"', '"browser"]']

    def test_non_string_scalar_is_stringified(self):
        assert _normalize_allowed_process_types(42) == ["42"]


# ------------------------------------------------------------- sanity checks

def test_alert_rules_contain_expected_metrics():
    # Не проверяем значения порогов (это бизнес-логика), только что структура
    # соответствует тому, на что опирается детектор и интеграционный тест.
    assert set(server_app.ALERT_RULES) == {"cpu_delta", "io_delta", "rss", "net_conn_count"}
    for rule in server_app.ALERT_RULES.values():
        assert {"K", "abs", "very_high_abs"} <= set(rule)

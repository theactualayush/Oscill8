"""
tests/test_ui_error_formatting.py

Coverage for ui.error_formatting.classify_scan_error() -- the pure
translation from a raised exception's type name + message into a
short, trader-facing headline/message, never a traceback or vendor
error code. Deliberately does NOT special-case the CORRA entitlement
gap documented in CLAUDE.md; these tests only exercise the general
keyword categories (permission/entitlement, no-data, connection) and
the generic fallback.
"""

from __future__ import annotations

from ui.error_formatting import GENERIC_ERROR, classify_scan_error


def test_permission_keyword_maps_to_data_access_message():
    presentation = classify_scan_error("LDError", "TS.Interday.UserNotPermission.70112")
    assert presentation.title == "⚠ Unable to fetch market data"
    assert "not available with the current data access" in presentation.message


def test_entitlement_keyword_maps_to_data_access_message():
    presentation = classify_scan_error("RuntimeError", "No entitlement for this instrument")
    assert presentation.title == "⚠ Unable to fetch market data"


def test_permission_word_alone_maps_to_data_access_message():
    presentation = classify_scan_error("Exception", "User does not have permission for this universe")
    assert presentation.title == "⚠ Unable to fetch market data"


def test_no_data_keyword_maps_to_data_availability_message():
    presentation = classify_scan_error("LDError", "No data to return, please check errors")
    assert presentation.title == "⚠ No market data available"
    assert "No data was returned" in presentation.message


def test_no_successful_response_keyword_maps_to_data_availability_message():
    presentation = classify_scan_error("RuntimeError", "No successful response received")
    assert presentation.title == "⚠ No market data available"


def test_connection_keyword_maps_to_connection_message():
    presentation = classify_scan_error("ConnectionError", "Connection refused")
    assert presentation.title == "⚠ Unable to connect to market data"
    assert "could not be reached" in presentation.message


def test_session_keyword_maps_to_connection_message():
    presentation = classify_scan_error("Exception", "No session established with the platform")
    assert presentation.title == "⚠ Unable to connect to market data"


def test_proxy_keyword_maps_to_connection_message():
    presentation = classify_scan_error("Exception", "Proxy authentication required")
    assert presentation.title == "⚠ Unable to connect to market data"


def test_unrecognized_error_falls_back_to_generic_message():
    presentation = classify_scan_error("ValueError", "something completely unexpected happened")
    assert presentation == GENERIC_ERROR
    assert presentation.title == "⚠ The scan could not be completed"


def test_classification_is_case_insensitive():
    presentation = classify_scan_error("Error", "USERNOTPERMISSION: access denied")
    assert presentation.title == "⚠ Unable to fetch market data"


def test_permission_category_is_checked_before_generic_fallback_even_with_extra_noise():
    # A realistic full exception message -- long, with punctuation and a
    # vendor error code -- still classifies correctly; nothing here
    # hard-codes CORRA or any other specific market name.
    presentation = classify_scan_error(
        "LDError",
        "Error code 70112 | TS.Interday.UserNotPermission.70112: "
        "User does not have permission for this universe.",
    )
    assert presentation.title == "⚠ Unable to fetch market data"


def test_no_presentation_ever_contains_exception_class_or_traceback_shaped_text():
    # A guard against regressing the core safety requirement: whatever
    # category is chosen, the returned title/message are fixed, curated
    # strings -- never an echo of the raw exception text itself.
    raw_message = "Traceback (most recent call last): File \"core/downloader.py\", line 118"
    presentation = classify_scan_error("SomeInternalException", raw_message)
    assert "Traceback" not in presentation.title
    assert "Traceback" not in presentation.message
    assert "downloader.py" not in presentation.title
    assert "downloader.py" not in presentation.message

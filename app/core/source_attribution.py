"""Shared source-attribution helpers for group files and inline media."""

from __future__ import annotations


def current_user_source_match(
    *,
    current_user_id: object = "",
    current_user_name: object = "",
    uploader_id: object = "",
    uploader_name: object = "",
) -> bool | None:
    """Return whether a source belongs to the current speaker.

    QQ/user IDs are authoritative when both sides have them. Nickname equality
    is only a weak fallback when neither side has a reliable ID; same nicknames
    across different users must not become a same-speaker fact.
    """
    current_uid = str(current_user_id or "")
    current_uname = str(current_user_name or "")
    source_uid = str(uploader_id or "")
    source_uname = str(uploader_name or "")
    if current_uid and source_uid:
        return current_uid == source_uid
    if current_uid or source_uid:
        return None
    if current_uname and source_uname:
        return current_uname == source_uname
    return None


def current_user_source_relation(match: bool | None) -> str:
    if match is True:
        return "same_speaker_upload"
    if match is False:
        return "other_user_upload"
    return "unknown_uploader_relation"

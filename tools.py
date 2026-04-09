from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


API_BASE = "https://platform-api.max.ru"
LOGGER = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 30

CALLBACK_SUFFIX_OPEN = "suffix:open"
CALLBACK_SUFFIX_CLEAR = "suffix:clear"
CALLBACK_SUFFIX_CANCEL = "suffix:cancel"
CALLBACK_CHANNELS_OPEN = "channels:open"
CALLBACK_CHANNELS_ADD = "channels:add"
CALLBACK_CHANNELS_REMOVE = "channels:remove"
CALLBACK_CHANNELS_CANCEL = "channels:cancel"
CALLBACK_MAIN_MENU = "menu:open"

TEXT_CANCEL_VALUES = {"cancel", "/cancel", "отмена", "/отмена"}
TEXT_CLEAR_SUFFIX_VALUES = {
    "clear",
    "/clear",
    "reset",
    "/reset",
    "none",
    "empty",
    "пусто",
    "очистить",
    "очистить суффикс",
    "/очистить",
    "/очистить_суффикс",
}


class MaxApiError(RuntimeError):
    pass


@dataclass(slots=True)
class PendingPost:
    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.text.strip() and not self.attachments

    def preview(self, limit: int = 160) -> str:
        normalized = " ".join(self.text.split())
        if not normalized:
            return "<без текста>"
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 3] + "..."


@dataclass(frozen=True, slots=True)
class ChannelTarget:
    chat_id: int
    title: str
    link: str | None = None
    description: str | None = None


class SuffixStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.touch(exist_ok=True)

    def load(self) -> str:
        suffix = self.path.read_text(encoding="utf-8").strip()
        LOGGER.debug("Loaded suffix from %s (length=%s)", self.path, len(suffix))
        return suffix

    def save(self, value: str) -> None:
        normalized = value.strip()
        self.path.write_text(normalized, encoding="utf-8")
        LOGGER.info("Saved suffix to %s (length=%s)", self.path, len(normalized))


class ChannelStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def initialize(self, channel_ids: set[int]) -> None:
        if self.exists():
            return
        self.save(channel_ids)
        LOGGER.info("Initialized channel store at %s with %s channel(s)", self.path, len(channel_ids))

    def load(self) -> set[int]:
        if not self.exists():
            return set()

        selected: set[int] = set()
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                selected.add(int(line))
            except ValueError:
                LOGGER.warning("Skipping invalid channel id in %s: %s", self.path, line)
        LOGGER.debug("Loaded %s selected channel id(s) from %s", len(selected), self.path)
        return selected

    def save(self, channel_ids: set[int]) -> None:
        lines = ["# Selected MAX channel ids"]
        lines.extend(str(chat_id) for chat_id in sorted(channel_ids))
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        LOGGER.info("Saved %s selected channel id(s) to %s", len(channel_ids), self.path)

    def load_valid(self, available_ids: set[int]) -> set[int]:
        selected = self.load()
        valid = selected & available_ids
        if selected != valid or not self.exists():
            self.save(valid)
        return valid


class MaxApiClient:
    def __init__(self, token: str) -> None:
        self.token = token.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": self.token,
                "Accept": "application/json",
                "User-Agent": "MAX_BOT/1.0",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        response = self.session.request(
            method=method,
            url=f"{API_BASE}{path}",
            params=params,
            json=json_body,
            timeout=timeout,
        )
        if response.ok:
            if not response.content:
                return {}
            return response.json()

        LOGGER.error(
            "MAX API request failed: %s %s -> %s | %s",
            method,
            path,
            response.status_code,
            response.text.strip(),
        )
        raise MaxApiError(
            f"MAX API request failed: {method} {path} -> {response.status_code}: {response.text.strip()}"
        )

    def get_me(self) -> dict[str, Any]:
        return self.request("GET", "/me")

    def get_updates(
        self,
        *,
        marker: int | None,
        update_types: list[str],
        limit: int,
        timeout: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "timeout": timeout,
        }
        if marker is not None:
            params["marker"] = marker
        if update_types:
            params["types"] = ",".join(update_types)
        return self.request("GET", "/updates", params=params, timeout=timeout + 5)

    def get_all_chats(self, *, count: int = 100) -> list[dict[str, Any]]:
        chats: list[dict[str, Any]] = []
        marker: int | None = None

        while True:
            params: dict[str, Any] = {"count": count}
            if marker is not None:
                params["marker"] = marker
            payload = self.request("GET", "/chats", params=params)
            chats.extend(payload.get("chats") or [])
            marker = payload.get("marker")
            if marker is None:
                return chats

    def get_chat_membership(self, chat_id: int) -> dict[str, Any]:
        return self.request("GET", f"/chats/{chat_id}/members/me")

    def send_message(
        self,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        text: str = "",
        attachments: list[dict[str, Any]] | None = None,
        notify: bool = True,
        fmt: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if user_id is not None:
            params["user_id"] = user_id
        if chat_id is not None:
            params["chat_id"] = chat_id

        body: dict[str, Any] = {
            "text": text or "",
            "notify": notify,
        }
        if attachments:
            body["attachments"] = attachments
        if fmt:
            body["format"] = fmt

        return self.request("POST", "/messages", params=params, json_body=body)

    def answer_callback(
        self,
        callback_id: str,
        *,
        message: dict[str, Any] | None = None,
        notification: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if message is not None:
            body["message"] = message
        if notification:
            body["notification"] = notification
        return self.request(
            "POST",
            "/answers",
            params={"callback_id": callback_id},
            json_body=body,
        )


def decode_escaped_newlines(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("\\r\\n", "\n").replace("\\n", "\n").strip()


def compose_post_text(text: str, suffix: str) -> str:
    base_text = text.strip()
    normalized_suffix = suffix.strip()
    if base_text and normalized_suffix:
        return f"{base_text}\n\n{normalized_suffix}"
    if base_text:
        return base_text
    return normalized_suffix


def extract_message_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    return decode_escaped_newlines(body.get("text"))


def is_private_dialog(message: dict[str, Any]) -> bool:
    recipient = message.get("recipient") or {}
    return recipient.get("chat_type") == "dialog"


def extract_image_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    _collect_image_attachments(message, result, seen)
    return result


def _collect_image_attachments(
    message: dict[str, Any],
    result: list[dict[str, Any]],
    seen: set[str],
) -> None:
    body = message.get("body") or {}
    for attachment in body.get("attachments") or []:
        if attachment.get("type") != "image":
            continue

        payload = attachment.get("payload") or {}
        outbound_payload: dict[str, Any] = {}

        token = payload.get("token")
        url = payload.get("url")
        photos = payload.get("photos")

        if token:
            key = f"token:{token}"
            outbound_payload["token"] = token
        elif url:
            key = f"url:{url}"
            outbound_payload["url"] = url
        elif photos:
            key = f"photos:{photos}"
            outbound_payload["photos"] = photos
        else:
            LOGGER.warning("Skipping image attachment without reusable payload: %s", attachment)
            continue

        if key in seen:
            continue

        seen.add(key)
        result.append({"type": "image", "payload": outbound_payload})

    linked_message = (message.get("link") or {}).get("message")
    if linked_message:
        _collect_image_attachments({"body": linked_message}, result, seen)


def fetch_admin_channels(client: MaxApiClient) -> list[ChannelTarget]:
    channels: list[ChannelTarget] = []

    for chat in client.get_all_chats():
        if chat.get("type") != "channel":
            continue
        if chat.get("status") != "active":
            continue

        chat_id = int(chat["chat_id"])
        membership = client.get_chat_membership(chat_id)
        permissions = set(membership.get("permissions") or [])
        if not membership.get("is_admin"):
            continue
        if permissions and "write" not in permissions:
            continue

        channels.append(
            ChannelTarget(
                chat_id=chat_id,
                title=str(chat.get("title") or f"channel_{chat_id}"),
                link=chat.get("link"),
                description=chat.get("description"),
            )
        )

    channels.sort(key=lambda item: (item.title.casefold(), item.chat_id))
    LOGGER.info("Discovered %s admin channel(s) available for posting", len(channels))
    return channels


def button_callback(text: str, payload: str, *, intent: str | None = None) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "callback",
        "text": text,
        "payload": payload,
    }
    if intent:
        button["intent"] = intent
    return button


def inline_keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "type": "inline_keyboard",
        "payload": {
            "buttons": rows,
        },
    }


def build_suffix_only_keyboard() -> list[dict[str, Any]]:
    return [
        inline_keyboard(
            [[button_callback("Суффикс", CALLBACK_SUFFIX_OPEN, intent="default")]]
        )
    ]


def build_post_publish_keyboard() -> list[dict[str, Any]]:
    return [
        inline_keyboard(
            [
                [
                    button_callback("Суффикс", CALLBACK_SUFFIX_OPEN, intent="default"),
                    button_callback(
                        "Изменить список каналов",
                        CALLBACK_CHANNELS_OPEN,
                        intent="default",
                    ),
                ]
            ]
        )
    ]


def build_suffix_input_keyboard() -> list[dict[str, Any]]:
    return [
        inline_keyboard(
            [
                [button_callback("Очистить суффикс", CALLBACK_SUFFIX_CLEAR, intent="negative")],
                [button_callback("Отмена ввода", CALLBACK_SUFFIX_CANCEL, intent="default")],
            ]
        )
    ]


def build_channels_editor_keyboard() -> list[dict[str, Any]]:
    return [
        inline_keyboard(
            [
                [
                    button_callback("Добавить канал", CALLBACK_CHANNELS_ADD, intent="positive"),
                    button_callback("Удалить канал", CALLBACK_CHANNELS_REMOVE, intent="negative"),
                ],
                [button_callback("К рассылке", CALLBACK_MAIN_MENU, intent="default")],
            ]
        )
    ]


def build_channels_cancel_keyboard() -> list[dict[str, Any]]:
    return [
        inline_keyboard(
            [[button_callback("Отмена", CALLBACK_CHANNELS_CANCEL, intent="default")]]
        )
    ]


def build_start_message() -> str:
    return (
        "Отправьте сообщение с текстом и картинками, и я разошлю его "
        "в выбранные каналы с вашим суффиксом."
    )


def build_suffix_prompt(suffix: str) -> str:
    suffix_preview = suffix if suffix else "<пусто>"
    return (
        f"Текущий суффикс:\n{suffix_preview}\n\n"
        "Следующим сообщением отправьте новый суффикс.\n"
        "Для очистки можно нажать кнопку ниже."
    )


def build_suffix_saved_message(suffix: str) -> str:
    suffix_preview = suffix if suffix else "<пусто>"
    return f"Суффикс обновлён:\n{suffix_preview}"


def build_suffix_cleared_message() -> str:
    return "Суффикс очищен."


def build_ready_to_send_message(suffix: str) -> str:
    suffix_preview = suffix if suffix else "<пусто>"
    return (
        "Отправьте сообщение с текстом и картинками.\n"
        f"Текущий суффикс: {suffix_preview}"
    )


def build_channels_editor_text(
    channels: list[ChannelTarget],
    selected_ids: set[int],
) -> str:
    if not channels:
        return "Бот пока не найден ни в одном канале, где у него есть права администратора."

    lines = ["Список каналов для рассылки:", ""]
    for index, channel in enumerate(channels, start=1):
        mark = "✅" if channel.chat_id in selected_ids else "❌"
        lines.append(f"{index}. {channel.title} ({channel.chat_id}) {mark}")
    return "\n".join(lines)


def build_channel_numbers_prompt(action_name: str) -> str:
    return (
        f"Отправьте через пробел номера каналов для действия «{action_name}».\n"
        "Пример: 1 3 5"
    )


def build_invalid_channel_numbers_message(max_index: int) -> str:
    return (
        "Не удалось распознать номера каналов.\n"
        f"Отправьте числа от 1 до {max_index} через пробел."
    )


def build_no_channels_message() -> str:
    return "Нет доступных каналов для редактирования."


def build_status_message(
    suffix: str,
    selected_channels: list[ChannelTarget],
    available_channels_count: int,
) -> str:
    suffix_preview = suffix if suffix else "<пусто>"
    selected_preview = ", ".join(channel.title for channel in selected_channels) or "<нет>"

    return (
        "Статус:\n"
        "Режим: мгновенная рассылка\n"
        f"Суффикс: {suffix_preview}\n"
        f"Каналы: {len(selected_channels)} из {available_channels_count}\n"
        f"Выбрано: {selected_preview}"
    )


def build_publish_result_message(success_lines: list[str], error_lines: list[str]) -> str:
    numbered_success = [f"{index}. {line}" for index, line in enumerate(success_lines, start=1)]

    if success_lines and not error_lines:
        return "Публикация выполнена:\n" + "\n".join(numbered_success)

    if success_lines and error_lines:
        return (
            "Публикация выполнена частично.\n\n"
            "Успешно:\n"
            + "\n".join(numbered_success)
            + "\n\nОшибки:\n"
            + "\n".join(error_lines)
        )

    return "Публикация не выполнена.\n\nОшибки:\n" + "\n".join(error_lines)


def parse_channel_numbers(text: str, max_index: int) -> tuple[list[int], list[str]]:
    tokens = text.replace(",", " ").split()
    if not tokens:
        return [], []

    indexes: list[int] = []
    invalid_tokens: list[str] = []
    seen: set[int] = set()

    for token in tokens:
        if not token.isdigit():
            invalid_tokens.append(token)
            continue

        value = int(token)
        if value < 1 or value > max_index:
            invalid_tokens.append(token)
            continue

        if value in seen:
            continue

        seen.add(value)
        indexes.append(value)

    return indexes, invalid_tokens


def publish_to_channels(
    client: MaxApiClient,
    channels: list[ChannelTarget],
    draft: PendingPost,
    suffix: str,
) -> tuple[list[str], list[str]]:
    post_text = compose_post_text(draft.text, suffix)
    success_lines: list[str] = []
    error_lines: list[str] = []

    LOGGER.info(
        "Publishing draft to %s channel(s): text_len=%s attachments=%s suffix_len=%s",
        len(channels),
        len(draft.text),
        len(draft.attachments),
        len(suffix),
    )

    for channel in channels:
        try:
            response = client.send_message(
                chat_id=channel.chat_id,
                text=post_text,
                attachments=draft.attachments,
            )
            message = response.get("message") or {}
            message_url = message.get("url")
            if message_url:
                success_lines.append(f"{channel.title}: {message_url}")
            elif channel.link:
                success_lines.append(f"{channel.title}: {channel.link}")
            else:
                success_lines.append(f"{channel.title}: {channel.chat_id}")
            LOGGER.info("Published successfully to %s (%s)", channel.title, channel.chat_id)
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("Failed to publish to chat_id=%s", channel.chat_id)
            error_lines.append(f"{channel.title}: {error}")

    LOGGER.info(
        "Publish finished: success=%s error=%s",
        len(success_lines),
        len(error_lines),
    )
    return success_lines, error_lines


def sleep_before_retry(seconds: int) -> None:
    time.sleep(seconds)

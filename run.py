from __future__ import annotations

import logging
from dataclasses import dataclass, field
from logging import Formatter, StreamHandler
from logging.handlers import RotatingFileHandler

from config import (
    ADMIN_USER_IDS,
    CHANNELS_FILE_PATH,
    LOG_BACKUP_COUNT,
    LOG_FILE_PATH,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    LOG_TO_FILE,
    LOG_TO_STDOUT,
    LONG_POLL_LIMIT,
    LONG_POLL_RETRY_DELAY_SECONDS,
    LONG_POLL_TIMEOUT_SECONDS,
    SUFFIX_FILE_PATH,
    TOKEN,
)
from tools import (
    CALLBACK_CHANNELS_ADD,
    CALLBACK_CHANNELS_CANCEL,
    CALLBACK_CHANNELS_OPEN,
    CALLBACK_CHANNELS_REMOVE,
    CALLBACK_MAIN_MENU,
    CALLBACK_SUFFIX_CANCEL,
    CALLBACK_SUFFIX_CLEAR,
    CALLBACK_SUFFIX_OPEN,
    ChannelStore,
    MaxApiClient,
    PendingPost,
    SuffixStore,
    TEXT_CANCEL_VALUES,
    TEXT_CLEAR_SUFFIX_VALUES,
    build_ready_to_send_message,
    build_channel_numbers_prompt,
    build_channels_cancel_keyboard,
    build_channels_editor_keyboard,
    build_channels_editor_text,
    build_invalid_channel_numbers_message,
    build_no_channels_message,
    build_post_publish_keyboard,
    build_start_message,
    build_status_message,
    build_suffix_cleared_message,
    build_suffix_input_keyboard,
    build_suffix_only_keyboard,
    build_suffix_prompt,
    build_suffix_saved_message,
    extract_image_attachments,
    extract_message_text,
    fetch_admin_channels,
    is_private_dialog,
    parse_channel_numbers,
    publish_to_channels,
    sleep_before_retry,
)


LOGGER = logging.getLogger(__name__)
ADMIN_ID_SET = {int(admin_id) for admin_id in ADMIN_USER_IDS}

START_COMMANDS = {"/start", "start", "/help", "help"}
STATUS_COMMANDS = {"/status", "status", "статус", "/статус"}
SUFFIX_COMMANDS = {"/suffix", "suffix", "суффикс", "/суффикс"}
CHANNELS_COMMANDS = {"/channels", "channels", "каналы", "/каналы"}
CANCEL_COMMANDS = {"/cancel", "cancel", "отмена", "/отмена"}

MODE_SUFFIX = "suffix"
MODE_CHANNELS_ADD = "channels_add"
MODE_CHANNELS_REMOVE = "channels_remove"


@dataclass(slots=True)
class AdminSession:
    awaiting_mode: str | None = None
    channel_snapshot_ids: list[int] = field(default_factory=list)


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    formatter = Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    if LOG_TO_STDOUT:
        console_handler = StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    if LOG_TO_FILE:
        file_handler = RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def validate_settings() -> None:
    if not TOKEN.strip():
        raise RuntimeError("TOKEN is empty")
    if not ADMIN_ID_SET:
        raise RuntimeError("ADMIN_USER_IDS is empty")


def get_or_create_session(
    sessions: dict[int, AdminSession],
    admin_id: int,
) -> AdminSession:
    session = sessions.get(admin_id)
    if session is None:
        session = AdminSession()
        sessions[admin_id] = session
    return session


def current_menu_keyboard(session: AdminSession) -> list[dict]:
    return build_suffix_only_keyboard()


def send_admin_message(
    client: MaxApiClient,
    admin_id: int,
    text: str,
    *,
    attachments: list[dict] | None = None,
) -> None:
    client.send_message(user_id=admin_id, text=text, attachments=attachments)


def acknowledge_callback(
    client: MaxApiClient,
    callback_id: str,
    *,
    notification: str | None = None,
) -> None:
    client.answer_callback(callback_id, notification=notification)


def send_start_message(
    client: MaxApiClient,
    admin_id: int,
) -> None:
    LOGGER.info("Sending start message to admin_id=%s", admin_id)
    send_admin_message(
        client,
        admin_id,
        build_start_message(),
        attachments=build_suffix_only_keyboard(),
    )


def enter_suffix_mode(
    client: MaxApiClient,
    admin_id: int,
    session: AdminSession,
    suffix_store: SuffixStore,
) -> None:
    session.awaiting_mode = MODE_SUFFIX
    LOGGER.info("Admin_id=%s entered suffix input mode", admin_id)
    send_admin_message(
        client,
        admin_id,
        build_suffix_prompt(suffix_store.load()),
        attachments=build_suffix_input_keyboard(),
    )


def open_channels_editor(
    client: MaxApiClient,
    admin_id: int,
    session: AdminSession,
    channel_store: ChannelStore,
    *,
    prefix_text: str | None = None,
) -> None:
    LOGGER.info("Opening channels editor for admin_id=%s", admin_id)
    channels = fetch_admin_channels(client)
    if not channels:
        session.channel_snapshot_ids = []
        send_admin_message(client, admin_id, build_no_channels_message())
        return

    available_ids = {channel.chat_id for channel in channels}
    channel_store.initialize(available_ids)
    selected_ids = channel_store.load_valid(available_ids)
    session.channel_snapshot_ids = [channel.chat_id for channel in channels]
    LOGGER.info(
        "Prepared channels editor for admin_id=%s with %s channel(s), selected=%s",
        admin_id,
        len(channels),
        len(selected_ids),
    )

    body = build_channels_editor_text(channels, selected_ids)
    if prefix_text:
        body = f"{prefix_text}\n\n{body}"

    send_admin_message(
        client,
        admin_id,
        body,
        attachments=build_channels_editor_keyboard(),
    )


def enter_channel_numbers_mode(
    client: MaxApiClient,
    admin_id: int,
    session: AdminSession,
    channel_store: ChannelStore,
    mode: str,
) -> None:
    channels = fetch_admin_channels(client)
    if not channels:
        session.awaiting_mode = None
        session.channel_snapshot_ids = []
        send_admin_message(client, admin_id, build_no_channels_message())
        return

    available_ids = {channel.chat_id for channel in channels}
    channel_store.initialize(available_ids)
    session.channel_snapshot_ids = [channel.chat_id for channel in channels]
    session.awaiting_mode = mode

    action_name = "добавить" if mode == MODE_CHANNELS_ADD else "удалить"
    LOGGER.info("Admin_id=%s entered channel edit mode=%s", admin_id, mode)
    send_admin_message(
        client,
        admin_id,
        build_channel_numbers_prompt(action_name),
        attachments=build_channels_cancel_keyboard(),
    )


def apply_channel_selection_change(
    client: MaxApiClient,
    admin_id: int,
    session: AdminSession,
    channel_store: ChannelStore,
    text: str,
) -> None:
    normalized = text.casefold()
    if normalized in TEXT_CANCEL_VALUES:
        LOGGER.info("Admin_id=%s canceled channel selection change", admin_id)
        session.awaiting_mode = None
        open_channels_editor(
            client,
            admin_id,
            session,
            channel_store,
            prefix_text="Изменения не внесены.",
        )
        return

    if not session.channel_snapshot_ids:
        session.awaiting_mode = None
        open_channels_editor(
            client,
            admin_id,
            session,
            channel_store,
            prefix_text="Список каналов обновился, откройте его заново.",
        )
        return

    indexes, invalid_tokens = parse_channel_numbers(text, len(session.channel_snapshot_ids))
    if not indexes or invalid_tokens:
        LOGGER.warning(
            "Admin_id=%s sent invalid channel numbers: text=%r invalid=%s",
            admin_id,
            text,
            invalid_tokens,
        )
        send_admin_message(
            client,
            admin_id,
            build_invalid_channel_numbers_message(len(session.channel_snapshot_ids)),
        )
        return

    channels = fetch_admin_channels(client)
    available_ids = {channel.chat_id for channel in channels}
    channel_store.initialize(available_ids)
    selected_ids = channel_store.load_valid(available_ids)

    chosen_ids = {
        session.channel_snapshot_ids[index - 1]
        for index in indexes
        if 0 <= index - 1 < len(session.channel_snapshot_ids)
    }
    chosen_ids &= available_ids

    if session.awaiting_mode == MODE_CHANNELS_ADD:
        updated_ids = selected_ids | chosen_ids
        prefix_text = "Каналы добавлены."
    else:
        updated_ids = selected_ids - chosen_ids
        prefix_text = "Каналы удалены."

    channel_store.save(updated_ids)
    LOGGER.info(
        "Admin_id=%s updated channels via mode=%s chosen=%s resulting_total=%s",
        admin_id,
        session.awaiting_mode,
        sorted(chosen_ids),
        len(updated_ids),
    )
    session.awaiting_mode = None
    open_channels_editor(
        client,
        admin_id,
        session,
        channel_store,
        prefix_text=prefix_text,
    )


def apply_suffix_input(
    client: MaxApiClient,
    admin_id: int,
    session: AdminSession,
    suffix_store: SuffixStore,
    text: str,
) -> None:
    normalized = text.casefold()

    if normalized in TEXT_CANCEL_VALUES:
        LOGGER.info("Admin_id=%s canceled suffix update", admin_id)
        session.awaiting_mode = None
        send_admin_message(
            client,
            admin_id,
            "Изменения не внесены.",
            attachments=current_menu_keyboard(session),
        )
        return

    if normalized in TEXT_CLEAR_SUFFIX_VALUES:
        LOGGER.info("Admin_id=%s cleared suffix", admin_id)
        suffix_store.save("")
        session.awaiting_mode = None
        send_admin_message(
            client,
            admin_id,
            build_suffix_cleared_message(),
            attachments=current_menu_keyboard(session),
        )
        return

    if not text.strip():
        LOGGER.info("Admin_id=%s sent empty suffix input, asking again", admin_id)
        send_admin_message(
            client,
            admin_id,
            build_suffix_prompt(suffix_store.load()),
            attachments=build_suffix_input_keyboard(),
        )
        return

    suffix_store.save(text)
    LOGGER.info("Admin_id=%s updated suffix", admin_id)
    session.awaiting_mode = None
    send_admin_message(
        client,
        admin_id,
        build_suffix_saved_message(text.strip()),
        attachments=current_menu_keyboard(session),
    )


def handle_callback(
    client: MaxApiClient,
    admin_id: int,
    callback_id: str,
    payload: str,
    sessions: dict[int, AdminSession],
    suffix_store: SuffixStore,
    channel_store: ChannelStore,
) -> None:
    session = get_or_create_session(sessions, admin_id)
    LOGGER.info("Received callback from admin_id=%s payload=%s", admin_id, payload)

    if payload == CALLBACK_SUFFIX_OPEN:
        acknowledge_callback(client, callback_id, notification="Ожидаю новый суффикс.")
        enter_suffix_mode(client, admin_id, session, suffix_store)
        return

    if payload == CALLBACK_SUFFIX_CLEAR:
        suffix_store.save("")
        session.awaiting_mode = None
        acknowledge_callback(client, callback_id, notification="Суффикс очищен.")
        send_admin_message(
            client,
            admin_id,
            build_suffix_cleared_message(),
            attachments=current_menu_keyboard(session),
        )
        return

    if payload == CALLBACK_SUFFIX_CANCEL:
        session.awaiting_mode = None
        acknowledge_callback(client, callback_id, notification="Изменения отменены.")
        send_admin_message(
            client,
            admin_id,
            "Изменения не внесены.",
            attachments=current_menu_keyboard(session),
        )
        return

    if payload == CALLBACK_CHANNELS_OPEN:
        acknowledge_callback(client, callback_id, notification="Открываю список каналов.")
        session.awaiting_mode = None
        open_channels_editor(client, admin_id, session, channel_store)
        return

    if payload == CALLBACK_MAIN_MENU:
        acknowledge_callback(client, callback_id, notification="Возвращаю к рассылке.")
        session.awaiting_mode = None
        send_admin_message(
            client,
            admin_id,
            build_ready_to_send_message(suffix_store.load()),
            attachments=build_post_publish_keyboard(),
        )
        return

    if payload == CALLBACK_CHANNELS_ADD:
        acknowledge_callback(client, callback_id, notification="Жду номера каналов.")
        enter_channel_numbers_mode(
            client,
            admin_id,
            session,
            channel_store,
            MODE_CHANNELS_ADD,
        )
        return

    if payload == CALLBACK_CHANNELS_REMOVE:
        acknowledge_callback(client, callback_id, notification="Жду номера каналов.")
        enter_channel_numbers_mode(
            client,
            admin_id,
            session,
            channel_store,
            MODE_CHANNELS_REMOVE,
        )
        return

    if payload == CALLBACK_CHANNELS_CANCEL:
        acknowledge_callback(client, callback_id, notification="Изменения отменены.")
        session.awaiting_mode = None
        open_channels_editor(
            client,
            admin_id,
            session,
            channel_store,
            prefix_text="Изменения не внесены.",
        )
        return

    acknowledge_callback(client, callback_id)


def handle_command(
    client: MaxApiClient,
    admin_id: int,
    session: AdminSession,
    text: str,
    suffix_store: SuffixStore,
    channel_store: ChannelStore,
) -> bool:
    normalized = text.casefold()
    if normalized:
        LOGGER.info("Handling command/text from admin_id=%s value=%s", admin_id, normalized)

    if normalized in START_COMMANDS:
        session.awaiting_mode = None
        send_start_message(client, admin_id)
        return True

    if normalized in STATUS_COMMANDS:
        channels = fetch_admin_channels(client)
        available_ids = {channel.chat_id for channel in channels}
        channel_store.initialize(available_ids)
        selected_ids = channel_store.load_valid(available_ids)
        selected_channels = [
            channel for channel in channels if channel.chat_id in selected_ids
        ]
        send_admin_message(
            client,
            admin_id,
            build_status_message(
                suffix=suffix_store.load(),
                selected_channels=selected_channels,
                available_channels_count=len(channels),
            ),
            attachments=current_menu_keyboard(session),
        )
        return True

    if normalized in SUFFIX_COMMANDS:
        enter_suffix_mode(client, admin_id, session, suffix_store)
        return True

    if normalized in CHANNELS_COMMANDS:
        session.awaiting_mode = None
        open_channels_editor(client, admin_id, session, channel_store)
        return True

    if normalized in CANCEL_COMMANDS:
        if session.awaiting_mode is not None:
            session.awaiting_mode = None
            send_admin_message(
                client,
                admin_id,
                "Изменения не внесены.",
                attachments=current_menu_keyboard(session),
            )
            return True

        send_admin_message(
            client,
            admin_id,
            "Сейчас нечего отменять.",
            attachments=current_menu_keyboard(session),
        )
        return True

    return False


def handle_admin_message(
    client: MaxApiClient,
    message: dict,
    sessions: dict[int, AdminSession],
    suffix_store: SuffixStore,
    channel_store: ChannelStore,
) -> None:
    sender = message.get("sender") or {}
    admin_id = int(sender["user_id"])

    if not is_private_dialog(message):
        LOGGER.info("Ignoring non-private admin message: user_id=%s", admin_id)
        return

    session = get_or_create_session(sessions, admin_id)
    text = extract_message_text(message)
    LOGGER.info(
        "Received admin message: admin_id=%s text_len=%s attachments=%s awaiting_mode=%s",
        admin_id,
        len(text),
        len((message.get("body") or {}).get("attachments") or []),
        session.awaiting_mode,
    )

    if session.awaiting_mode == MODE_SUFFIX:
        apply_suffix_input(client, admin_id, session, suffix_store, text)
        return

    if session.awaiting_mode in {MODE_CHANNELS_ADD, MODE_CHANNELS_REMOVE}:
        apply_channel_selection_change(client, admin_id, session, channel_store, text)
        return

    if handle_command(client, admin_id, session, text, suffix_store, channel_store):
        return

    attachments = extract_image_attachments(message)
    post = PendingPost(text=text, attachments=attachments)
    if post.is_empty():
        send_admin_message(
            client,
            admin_id,
            "Сообщение пустое или не содержит поддерживаемых картинок.",
            attachments=current_menu_keyboard(session),
        )
        return

    channels = fetch_admin_channels(client)
    available_ids = {channel.chat_id for channel in channels}
    channel_store.initialize(available_ids)
    selected_ids = channel_store.load_valid(available_ids)
    selected_channels = [
        channel for channel in channels if channel.chat_id in selected_ids
    ]

    if not selected_channels:
        LOGGER.info("Admin_id=%s attempted immediate publish without selected channels", admin_id)
        send_admin_message(
            client,
            admin_id,
            "Нет выбранных каналов. Откройте список каналов и добавьте нужные.",
            attachments=build_post_publish_keyboard(),
        )
        return

    LOGGER.info(
        "Immediate publish for admin_id=%s text_len=%s images=%s channels=%s",
        admin_id,
        len(post.text),
        len(post.attachments),
        len(selected_channels),
    )
    success_lines, error_lines = publish_to_channels(
        client=client,
        channels=selected_channels,
        draft=post,
        suffix=suffix_store.load(),
    )
    LOGGER.info(
        "Immediate publish by admin_id=%s finished: success=%s error=%s",
        admin_id,
        len(success_lines),
        len(error_lines),
    )
    send_admin_message(
        client,
        admin_id,
        build_publish_result_message(success_lines, error_lines),
        attachments=build_post_publish_keyboard(),
    )


def main() -> None:
    validate_settings()
    client = MaxApiClient(TOKEN)
    me = client.get_me()
    suffix_store = SuffixStore(SUFFIX_FILE_PATH)
    channel_store = ChannelStore(CHANNELS_FILE_PATH)
    sessions: dict[int, AdminSession] = {}
    marker: int | None = None

    startup_channels = fetch_admin_channels(client)
    channel_store.initialize({channel.chat_id for channel in startup_channels})

    LOGGER.info("Bot user_id=%s", me.get("user_id"))
    LOGGER.info("Admin ids=%s", ", ".join(str(item) for item in sorted(ADMIN_ID_SET)))
    LOGGER.info("Detected channels=%s", len(startup_channels))
    LOGGER.info(
        "Runtime settings: poll_limit=%s poll_timeout=%s retry_delay=%s log_level=%s log_to_file=%s log_to_stdout=%s",
        LONG_POLL_LIMIT,
        LONG_POLL_TIMEOUT_SECONDS,
        LONG_POLL_RETRY_DELAY_SECONDS,
        LOG_LEVEL,
        LOG_TO_FILE,
        LOG_TO_STDOUT,
    )
    LOGGER.info("Paths: channels=%s suffix=%s log=%s", CHANNELS_FILE_PATH, SUFFIX_FILE_PATH, LOG_FILE_PATH)

    while True:
        try:
            payload = client.get_updates(
                marker=marker,
                update_types=["message_created", "message_callback", "bot_started"],
                limit=LONG_POLL_LIMIT,
                timeout=LONG_POLL_TIMEOUT_SECONDS,
            )
            marker = payload.get("marker", marker)

            for update in payload.get("updates") or []:
                update_type = update.get("update_type")

                if update_type == "message_created":
                    message = update.get("message") or {}
                    sender = message.get("sender") or {}
                    user_id = sender.get("user_id")
                    if user_id not in ADMIN_ID_SET:
                        continue
                    handle_admin_message(
                        client=client,
                        message=message,
                        sessions=sessions,
                        suffix_store=suffix_store,
                        channel_store=channel_store,
                    )
                    continue

                if update_type == "bot_started":
                    user = update.get("user") or {}
                    user_id = user.get("user_id")
                    if user_id not in ADMIN_ID_SET:
                        continue
                    send_start_message(client, int(user_id))
                    continue

                if update_type == "message_callback":
                    callback = update.get("callback") or {}
                    callback_id = callback.get("callback_id")
                    payload_value = str(callback.get("payload") or "")
                    user = callback.get("user") or {}
                    user_id = user.get("user_id")
                    if user_id not in ADMIN_ID_SET or not callback_id:
                        continue

                    handle_callback(
                        client=client,
                        admin_id=int(user_id),
                        callback_id=str(callback_id),
                        payload=payload_value,
                        sessions=sessions,
                        suffix_store=suffix_store,
                        channel_store=channel_store,
                    )
        except KeyboardInterrupt:
            LOGGER.info("Bot stopped by user")
            raise
        except Exception:  # noqa: BLE001
            LOGGER.exception("Long polling loop crashed")
            sleep_before_retry(LONG_POLL_RETRY_DELAY_SECONDS)


if __name__ == "__main__":
    configure_logging()
    main()

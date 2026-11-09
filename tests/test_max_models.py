from __future__ import annotations

import logging

from matele.max import models as m
from tests.max_fake import message, user


def test_polymorphic_update_and_nested_attachments():
	raw = {
		"update_type": "message_created",
		"timestamp": 1,
		"message": message(
			attachments=[
				{"type": "image", "payload": {"photo_id": 5, "token": "tok", "url": "https://i/1.jpg"}},
				{"type": "file", "payload": {"url": "https://f/1", "token": "ftok"}, "filename": "a.pdf", "size": 10},
				{
					"type": "inline_keyboard",
					"payload": {"buttons": [[{"type": "callback", "text": "Go", "payload": "go"}]]},
				},
			]
		),
		"user_locale": "ru",
	}
	update = m.Update.from_dict(raw)
	assert isinstance(update, m.MessageCreatedUpdate)
	assert update.user_locale == "ru" and update.raw is raw
	photo, file, keyboard = update.message.body.attachments or []
	assert isinstance(photo, m.PhotoAttachment) and photo.payload.token == "tok"
	assert isinstance(file, m.FileAttachment) and file.filename == "a.pdf" and file.payload.token == "ftok"
	assert isinstance(keyboard, m.InlineKeyboardAttachment)
	button = keyboard.payload.buttons[0][0]
	assert isinstance(button, m.CallbackButton) and button.payload == "go"


def test_unknown_types_and_fields_degrade_gracefully():
	update = m.Update.from_dict({"update_type": "poll_created", "timestamp": 1, "poll": {"id": 1}})
	assert type(update) is m.Update and update.update_type == "poll_created"
	assert update.raw is not None and update.raw["poll"] == {"id": 1}
	att = m.Attachment.from_dict({"type": "story", "payload": {"x": 1}})
	assert type(att) is m.Attachment and att.type == "story"
	u = m.User.from_dict(user(new_field="whatever"))
	assert u.first_name == "Ann" and u.raw is not None and u.raw["new_field"] == "whatever"


def test_missing_required_field_becomes_none(caplog):
	caplog.set_level(logging.DEBUG, "matele.max._model")
	u = m.User.from_dict({"user_id": 1, "is_bot": False})
	assert u.first_name is None  # type: ignore[comparison-overlap]
	assert "without required field 'first_name'" in caplog.text


def test_keyword_field_names_round_trip():
	markup = m.MarkupElement.from_dict({"type": "strong", "from": 3, "length": 4})
	assert isinstance(markup, m.StrongMarkup) and markup.from_ == 3
	assert markup.to_dict() == {"type": "strong", "from": 3, "length": 4}


def test_encoding_skips_none_but_keeps_empty_lists():
	body = m.NewMessageBody(text="hi", attachments=[], format="html")
	assert body.to_dict() == {"text": "hi", "attachments": [], "format": "html"}
	kb = m.InlineKeyboardAttachmentRequest(
		payload=m.InlineKeyboardAttachmentRequestPayload(
			buttons=[[m.CallbackButton(text="A", payload="a"), m.LinkButton(text="L", url="https://x.ru")]]
		)
	)
	assert kb.to_dict() == {
		"type": "inline_keyboard",
		"payload": {
			"buttons": [
				[
					{"type": "callback", "text": "A", "payload": "a"},
					{"type": "link", "text": "L", "url": "https://x.ru"},
				]
			]
		},
	}


def test_every_family_maps_to_subclasses_of_its_base():
	for base in (m.Attachment, m.AttachmentRequest, m.Button, m.MarkupElement, m.Update):
		tag_field = base.__tag__
		assert tag_field and base.__subtypes__
		for tag, cls in base.__subtypes__.items():
			assert issubclass(cls, base)
			assert cls.__dataclass_fields__[tag_field].default == tag  # type: ignore[attr-defined]
			assert type(base.from_dict({tag_field: tag})) is cls

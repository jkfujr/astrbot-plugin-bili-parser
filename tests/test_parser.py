import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(WORKSPACE_ROOT / "AstrBot"))

original_cwd = Path.cwd()
os.chdir(WORKSPACE_ROOT)
try:
    parser_module = importlib.import_module("astrbot-plugin-bili-parser.core.parser")
    main_module = importlib.import_module("astrbot-plugin-bili-parser.main")
    components_module = importlib.import_module("astrbot.api.message_components")
finally:
    os.chdir(original_cwd)

BiliLinkParser = parser_module.BiliLinkParser
Link = parser_module.Link
BiliParser = main_module.BiliParser
Comp = components_module


QQ_IMAGE_URL = (
    "https://multimedia.nt.qq.com.cn/download?appid=1407"
    "&fileid=EhSUAmFgP_QAUTxcbWSezLADRJbruxj23Qsg_woo"
    "&rkey=CAMSMG3KWj0TUVJ2vN2dPTN8FRhHp5uicqKlObi_AU4__Xd43UIYx"
)


def make_parser(audio_full_url=False):
    return BiliLinkParser(
        {
            "audio": {"enable": True, "full_url": audio_full_url},
            "short_link": {"enable": True},
        }
    )


def test_card_ignores_qq_image_url_with_audio_token():
    parser = make_parser(audio_full_url=False)

    links = parser.extract_card_links(
        {"message": [{"type": "image", "data": {"url": QQ_IMAGE_URL}}]}
    )

    assert links == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.bilibili.com/video/BV17M3R61EuZ/", Link("Video", "BV17M3R61EuZ")),
        ("https://live.bilibili.com/12345", Link("Live", "12345")),
        ("https://www.bilibili.com/read/cv67890", Link("Article", "67890")),
        ("https://www.bilibili.com/audio/au12345", Link("Audio", "12345")),
        ("https://b23.tv/AbC123", Link("Short", "AbC123")),
    ],
)
def test_card_extracts_supported_full_urls(url, expected):
    parser = make_parser(audio_full_url=False)

    assert parser.extract_card_links({"url": url}) == [expected]


def test_card_extracts_url_from_stringified_nested_json():
    parser = make_parser()
    payload = {
        "data": json.dumps(
            {"jumpUrl": "https://www.bilibili.com/video/BV17M3R61EuZ/"}
        )
    }

    assert parser.extract_card_links(payload) == [Link("Video", "BV17M3R61EuZ")]


@pytest.mark.parametrize(
    "url",
    [
        QQ_IMAGE_URL,
        "https://example.com/download_AU4_",
        "https://evilbilibili.com/video/BV17M3R61EuZ/",
        "www.bilibili.com/audio/au12345",
    ],
)
def test_card_rejects_non_bili_or_incomplete_urls(url):
    parser = make_parser(audio_full_url=False)

    assert parser.extract_card_links({"url": url}) == []


def test_text_keeps_bare_audio_id_configuration():
    assert make_parser(audio_full_url=False).extract_links("audio au12345") == [
        Link("Audio", "12345")
    ]
    assert make_parser(audio_full_url=True).extract_links("audio au12345") == []


class FakeEvent:
    def __init__(self, message_str, messages, raw_message):
        self.message_str = message_str
        self._messages = messages
        self.message_obj = SimpleNamespace(raw_message=raw_message)

    def get_messages(self):
        return self._messages


def make_plugin(parser):
    plugin = object.__new__(BiliParser)
    plugin.config = {
        "json_card": {"enable": True},
        "basic": {"parse_limit": 3},
        "short_link": {"enable": False},
    }
    plugin.parser = parser
    return plugin


@pytest.mark.asyncio
async def test_event_does_not_scan_raw_message():
    parser = make_parser(audio_full_url=False)
    plugin = make_plugin(parser)
    event = FakeEvent("", [], {"message": [{"data": {"url": QQ_IMAGE_URL}}]})

    assert await plugin._extract_event_links(event, debug=False) == []


@pytest.mark.asyncio
async def test_event_extracts_normalized_json_component():
    parser = make_parser(audio_full_url=False)
    plugin = make_plugin(parser)
    component = Comp.Json(
        data={"url": "https://www.bilibili.com/video/BV17M3R61EuZ/"}
    )
    event = FakeEvent("", [component], {"url": QQ_IMAGE_URL})

    assert await plugin._extract_event_links(event, debug=False) == [
        Link("Video", "BV17M3R61EuZ")
    ]


@pytest.mark.asyncio
async def test_short_link_redirect_uses_full_url_patterns():
    parser = make_parser(audio_full_url=False)

    class APIClient:
        async def get_short_redir_url(self, link_id):
            assert link_id == "AbC123"
            return "https://www.bilibili.com/video/BV17M3R61EuZ/"

    links = await parser.resolve_short_links([Link("Short", "AbC123")], APIClient())

    assert links == [Link("Video", "BV17M3R61EuZ")]

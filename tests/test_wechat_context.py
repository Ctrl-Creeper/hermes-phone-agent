from plugins.phone_use.backend import CaptureResult, UIElement
from plugins.phone_use import wechat_context as wc


def _cap(header_text):
    # Chat screen: title bar + a bottom message input, as collect_context sees it.
    return CaptureResult(
        mode="image_hierarchy", width=1080, height=2400,
        current_package="com.tencent.mm", current_activity=".ui.LauncherUI",
        elements=[
            UIElement(index=1, class_name="host.ocr.Text", text=header_text,
                      bounds=(289, 107, 760, 161)),
            UIElement(index=2, class_name="host.ocr.MessageInput", text="",
                      bounds=(110, 2165, 720, 2325)),
        ],
    )


def test_verified_chat_tolerates_ocr_substitution_in_the_title():
    # OCR reads "AIchemy" as "Alchemy"; the chat is still the same one.
    assert wc._verified_chat(_cap("炼丹社 Alchemy（164）"), "炼丹社 AIchemy")


def test_verified_chat_rejects_a_different_chat():
    assert not wc._verified_chat(_cap("完全不同的群（9）"), "炼丹社 AIchemy")


def test_verified_chat_rejects_a_similarly_named_chat_with_an_extra_char():
    # An inserted character is a different chat, not an OCR glyph swap.
    assert not wc._verified_chat(_cap("Examples"), "Example")
    assert not wc._verified_chat(_cap("Example Group"), "Example")


def test_same_chat_title_boundaries():
    assert wc._same_chat_title("炼丹社 Alchemy（164）", "炼丹社 AIchemy")   # glyph swap
    assert not wc._same_chat_title("Examples", "Example")                # inserted char
    assert not wc._same_chat_title("abcd", "abce")                       # too short to fuzz
    assert wc._same_chat_title("Example", "Example")                     # exact

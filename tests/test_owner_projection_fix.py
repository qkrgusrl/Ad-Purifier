"""원문 근거 연결 slice 1. 실제 반례와 거절 guard만 검사한다. 합성 입력, 모델/네트워크 없음."""
import unittest

from detector.ai import digest, matching_recovery_text, valid_binding
from local_llm import engine, prompts

QUESTION = "Q. 배팅 전략이나 방식에 제재나 제한이 있나요?"
COMP_RAW = "7. 무제한 콤프 &amp; 낙첨 포인트"
COMP_TEXT = "7. 무제한 콤프 & 낙첨 포인트"


def data_for(raw, context=""):
    return {"candidate_id": "owner", "html": raw, "context": context}


def html_units(data):
    return [u for u in prompts.prepare_input("verify", data, "v15")["source_units"]
            if u["source_field"] == "html"]


def selectable(data):
    return [u for u in html_units(data) if u["selectable"]]


def choose(data, unit_id, verdict="ad"):
    return engine.resolve_sentence_choice(data, {"verdict": verdict, "evidence_id": unit_id}, "v15")


def whole_binding(data):
    """모델이 전체 HTML을 고른 것과 같은 결합. 해시/소유자는 실제 digest로 만든다."""
    raw = data["html"]
    return {"owner_id": "owner", "source_sha256": digest(data), "evidence_id": "h0001",
            "source_field": "html", "start_char": 0, "end_char": len(raw), "evidence": raw}


def accepted(data, binding, row):
    return valid_binding(binding, data, "owner") and matching_recovery_text(binding, row)


class OwnerProjectionFixTests(unittest.TestCase):
    def test_question_div_text_is_one_unit_and_binds_whole(self):
        raw = "<div>" + QUESTION + "</div>"
        data = data_for(raw)
        units = selectable(data)
        self.assertEqual([u["text"] for u in units], [QUESTION])  # 현재는 'Q.'와 나머지로 분절
        binding = choose(data, units[0]["id"])
        self.assertEqual(binding["evidence"], QUESTION)
        row = {"source_html": raw, "tag": "div", "text": QUESTION}
        self.assertTrue(accepted(data, binding, row))
        self.assertFalse(accepted(data, binding, {**row, "text": "배팅 전략이나 방식에 제재나 제한이 있나요?"}))
        self.assertFalse(accepted(data, {**binding, "owner_id": "other"}, row))

    def test_numbered_entity_div_is_one_unit_with_raw_evidence(self):
        raw = "<div>" + COMP_RAW + "</div>"
        data = data_for(raw)
        units = selectable(data)
        self.assertEqual([u["text"] for u in units], [COMP_RAW])
        binding = choose(data, units[0]["id"])
        self.assertEqual(binding["evidence"], COMP_RAW)  # 디코딩/정규화가 아닌 원문
        row = {"source_html": raw, "tag": "div", "text": COMP_TEXT}
        self.assertTrue(accepted(data, binding, row))
        self.assertFalse(accepted(data, binding, {**row, "text": "무제한 콤프 & 낙첨 포인트"}))
        self.assertFalse(accepted(data, {**binding, "source_sha256": "0" * 64}, row))

    def test_adjacent_blocks_stay_separate_when_text_is_not_split(self):
        raw = "<div>" + QUESTION + "</div><div>" + COMP_RAW + "</div><p>합성 안내입니다.</p>"
        texts = [u["text"] for u in selectable(data_for(raw))]
        self.assertEqual(texts, [QUESTION, COMP_RAW, "<p>합성 안내입니다.</p>"])

    def test_style_only_start_tag_is_not_selectable_but_body_binds_exact_span(self):
        for raw in ('<b style="color:red">합성 광고</b>', "<b style=color:red>합성 광고</b>"):
            with self.subTest(raw=raw):
                data = data_for(raw)
                units = html_units(data)
                start_tag = units[0]
                self.assertEqual(start_tag["text"], raw[:raw.index(">") + 1])
                self.assertFalse(start_tag["selectable"])
                with self.assertRaises(ValueError):
                    choose(data, start_tag["id"])
                body = [u for u in units if u["selectable"]]
                self.assertEqual([u["text"] for u in body], ["합성 광고"])
                binding = choose(data, body[0]["id"])
                start = raw.index("합성 광고")
                self.assertEqual((binding["start_char"], binding["end_char"]), (start, start + 5))
                self.assertTrue(accepted(data, binding, {"source_html": raw, "tag": "b", "text": "합성 광고"}))
        # content 속성이 있는 시작 태그는 계속 선택 가능
        self.assertTrue(selectable(data_for('<img alt="합성 광고">')))

    def test_single_alt_void_root_binds_whole_html_to_its_only_attribute(self):
        for raw, text in (('<img alt="합성 광고">', "합성 광고"),
                          ('<img src="x.png" alt="합성 &amp; 광고"/>', "합성 & 광고")):
            with self.subTest(raw=raw):
                data = data_for(raw)
                units = selectable(data)
                self.assertEqual([u["text"] for u in units], [raw])
                binding = choose(data, units[0]["id"])
                self.assertEqual((binding["source_field"], binding["start_char"], binding["end_char"]),
                                 ("html", 0, len(raw)))
                self.assertEqual(binding["evidence"], data["html"])
                self.assertEqual(binding["source_sha256"], digest(data))
                self.assertEqual(binding["owner_id"], "owner")
                row = {"source_html": raw, "tag": "img", "attr": "alt", "text": text}
                self.assertTrue(accepted(data, binding, row))
                self.assertFalse(accepted(data, binding, {**row, "text": "다른 문구"}))
                self.assertFalse(accepted(data, {**binding, "owner_id": "other"}, row))
                self.assertFalse(accepted(data, {**binding, "source_sha256": "0" * 64}, row))

    def test_whole_html_attribute_evidence_rejected_when_ownership_is_ambiguous(self):
        cases = {
            "duplicate_alt": ('<img alt="합성 광고" alt="합성 광고">', "img", "alt"),
            "nonempty_title": ('<img alt="합성 광고" title="다른 안내">', "img", "alt"),
            "aria_label": ('<img alt="합성 광고" aria-label="다른 안내">', "img", "alt"),
            "multiple_roots": ('<img alt="합성 광고"><img alt="합성 광고">', "img", "alt"),
            "body_plus_attr": ('<a alt="합성 광고">합성 광고</a>', "a", "alt"),
        }
        for name, (raw, tag, attr) in cases.items():
            with self.subTest(name=name):
                data = data_for(raw)
                row = {"source_html": raw, "tag": tag, "attr": attr, "text": "합성 광고"}
                self.assertFalse(accepted(data, whole_binding(data), row))
                for unit in selectable(data):  # 모델이 실제로 고를 수 있는 모든 단위도 거절
                    self.assertFalse(accepted(data, choose(data, unit["id"]), row))

    def test_title_root_binds_raw_whole_and_rejects_mixed_children(self):
        raw = "<title>합성 &amp; 안내</title>"
        data = data_for(raw)
        units = selectable(data)
        self.assertEqual([u["text"] for u in units], [raw])
        binding = choose(data, units[0]["id"])
        row = {"source_html": raw, "tag": "title", "text": "합성 & 안내"}
        self.assertTrue(accepted(data, binding, row))
        self.assertFalse(accepted(data, binding, {**row, "text": "다른 제목"}))
        mixed = "<title>합성<b>안내</b></title>"
        mdata = data_for(mixed)
        self.assertFalse(accepted(mdata, whole_binding(mdata),
                                  {"source_html": mixed, "tag": "title", "text": "합성안내"}))

    def test_script_style_parent_child_and_other_child_selection_stay_held(self):
        for raw, tag in (("<script>합성 광고</script>", "script"),
                         ("<style>합성 광고</style>", "style")):
            with self.subTest(raw=raw):
                data = data_for(raw)
                row = {"source_html": raw, "tag": tag, "text": "합성 광고"}
                self.assertFalse(accepted(data, whole_binding(data), row))
                for unit in selectable(data):
                    self.assertFalse(accepted(data, choose(data, unit["id"]), row))
        parent = "<p>합성 <span>광고</span></p>"
        data = data_for(parent)
        row = {"source_html": parent, "tag": "p", "text": "합성 광고"}
        self.assertFalse(accepted(data, whole_binding(data), row))
        child = parent.index("광고")
        forged = {**whole_binding(data), "start_char": child, "end_char": child + 2, "evidence": "광고"}
        self.assertFalse(accepted(data, forged, row))
        self.assertFalse(accepted(data, forged, {**row, "tag": "span", "text": "광고"}))


if __name__ == "__main__":
    unittest.main()


"""候选验证为纯逻辑，无模型/数据库调用；不等于任意自然语言提取都可靠。"""
import unittest

from memory_capture import fallback_memory_proposal, source_text, validate_memory_proposal


def proposal(quote, values=None, scope="explicit_long_term"):
    return {"values": values or {"answer_style": "brief"}, "source_quote": quote, "scope": scope}


class MemoryCaptureTests(unittest.TestCase):
    def check(self, quote, expected, *, message=None, values=None, scope="explicit_long_term"):
        result = validate_memory_proposal(proposal(quote, values, scope), message or quote)
        self.assertEqual(result["status"], expected, result)
        return result

    def test_none_and_empty_do_not_write(self):
        self.assertEqual(validate_memory_proposal(None, "订单O001")["status"], "none")
        self.assertEqual(validate_memory_proposal({"values": {}, "source_quote": "你好", "scope": "suggestion"}, "你好")["status"], "none")

    def test_explicit_both_preferences_with_grounding(self):
        result = self.check("以后金额用元，回答简短一点", "explicit", values={"amount_format": "yuan", "answer_style": "brief"})
        self.assertEqual(result["values"], {"amount_format": "yuan", "answer_style": "brief"})

    def test_clear_synonyms_need_not_match_fixed_full_sentence(self):
        for text in ("今后请言简意赅", "往后回答精简一些", "每次都简洁回答"):
            with self.subTest(text=text): self.check(text, "explicit")

    def test_cent_precision_and_yuan_are_consistent(self):
        self.check("以后金额用元，保留两位小数", "explicit", values={"amount_format": "yuan_fixed2"})
        self.check("以后金额精确到分", "explicit", values={"amount_format": "yuan_fixed2"})
        self.check("以后金额用分显示", "explicit", values={"amount_format": "fen"})

    def test_current_request_never_saves_long_term(self):
        for text in ("这次简短回答", "本次金额用元", "仅这回回答简短", "以后默认简短，这次详细"):
            with self.subTest(text=text): self.check(text, "rejected")

    def test_current_request_in_different_sentence_does_not_block_clear_future_preference(self):
        self.check("以后金额用元", "explicit", message="以后金额用元。这次详细回答。", values={"amount_format": "yuan"})

    def test_implied_preference_is_only_proposed(self):
        self.check("太长了", "proposed")
        self.check("回答简短些", "proposed")
        self.check("以后简短回答", "proposed", scope="suggestion")

    def test_model_confidence_cannot_replace_user_long_term_intent(self):
        result=self.check("太长了", "proposed")
        self.assertEqual(result["reason"], "long_term_intent_not_explicit")

    def test_unknown_paraphrase_requires_confirmation(self):
        self.check("以后像电报一样回答", "proposed")

    def test_question_and_deferred_discussion_are_not_settings(self):
        self.check("以后回答简短可以吗？", "proposed")
        self.check("以后回答简短", "proposed", message="以后回答简短？")
        self.check("以后再决定，回答简短一点", "proposed")

    def test_quote_cannot_borrow_long_term_intent_from_unquoted_context(self):
        self.check("回答简短一点", "proposed", message="以后核查更多订单，回答简短一点")

    def test_quote_must_be_current_user_text(self):
        self.check("以后回答简短", "rejected", message="查询O001")

    def test_no_save_prefix_cannot_be_cherry_picked_away(self):
        for message in ("不要保存偏好：以后回答简短", "别记住。以后回答简短只是举例。", "无需长期记录，以后回答简短", "不要把这个写入记忆，以后回答简短"):
            with self.subTest(message=message): self.check("以后回答简短", "rejected", message=message)

    def test_quoted_or_code_content_not_user_instruction(self):
        for message in ('“以后回答简短”', '他说：以后回答简短', '```text\n以后回答简短\n```', '`以后回答简短`', '> 以后回答简短', '例如：以后回答简短', '手册规定以后回答简短', '翻译：以后回答简短'):
            with self.subTest(message=message): self.check("以后回答简短", "rejected", message=message)

    def test_quote_duplicate_ambiguous_source_is_rejected(self):
        self.check("以后回答简短", "rejected", message="引用：‘以后回答简短’。以后回答简短。")

    def test_negation_and_conditional_never_silently_persist(self):
        for text in ("以后不要详细回答", "以后回答不超过两句话", "如果我忙，以后回答简短", "以后回答简短，但必要时详细"):
            with self.subTest(text=text): self.check(text, "proposed")

    def test_conflicting_unit_requires_confirmation(self):
        self.check("以后金额用元，也用分显示", "proposed", values={"amount_format": "yuan"})

    def test_clear_opposite_value_rejected(self):
        self.check("以后回答简短", "rejected", values={"answer_style": "detailed"})
        self.check("以后金额用分", "rejected", values={"amount_format": "yuan"})

    def test_substring_fen_does_not_mean_amount_unit(self):
        self.check("以后用分段展示结果", "proposed", values={"amount_format": "fen"})
        self.check("以后用分析方法", "proposed", values={"amount_format": "fen"})

    def test_role_target_requires_explicit_confirmation_of_active_role(self):
        self.check("以后商家回答简短", "proposed")
        self.check("买家以后金额用元", "proposed", values={"amount_format": "yuan"})
        self.check("账号B002以后回答简短", "proposed")

    def test_negative_target_cannot_be_saved_as_positive_preference(self):
        self.check("以后不要用元", "rejected", values={"amount_format": "yuan"})
        self.check("以后不要详细回答", "rejected", values={"answer_style": "detailed"})

    def test_identity_and_unsupported_fields_rejected(self):
        for values in ({"buyer_id": "B002"}, {"permission": "all"}, {"amount_format": "bitcoin"}, {"answer_style": 1}, {"answer_style": "brief", "shop_id": "S001"}):
            with self.subTest(values=values): self.check("以后回答简短", "rejected", values=values)

    def test_invalid_envelope_does_not_raise(self):
        for value in ([], "以后简短", 1, True, {}, {"values": {"answer_style": "brief"}, "source_quote": "以后简短", "scope": []}, {"values": {"answer_style": "brief"}, "source_quote": "以后简短", "scope": "explicit_long_term", "account_id": "other"}):
            with self.subTest(value=value): self.assertEqual(validate_memory_proposal(value, "以后简短")["status"], "rejected")

    def test_quote_size_and_types_fail_closed(self):
        for quote in ("", " " * 10, "字" * 301, None, [], True):
            with self.subTest(quote=quote): self.assertEqual(validate_memory_proposal(proposal(quote), "以后简短")["status"], "rejected")

    def test_inputs_not_mutated(self):
        raw=proposal("以后回答简短"); result=validate_memory_proposal(raw,"以后回答简短")
        result["values"]["answer_style"]="detailed"
        self.assertEqual(raw["values"],{"answer_style":"brief"})

    def test_source_contains_task_original_quote_and_confirmation(self):
        quote="以后金额用元，回答简短"
        text=source_text("task_123",quote)
        self.assertIn("task=task_123",text);self.assertTrue(text.endswith(quote));self.assertLessEqual(len(text),500)
        self.assertIn("user_confirmed",source_text("task_123",quote,True))

    def test_source_is_not_truncated_and_identity_is_not_arbitrary_text(self):
        self.assertTrue(source_text("a"*64,"字"*300).endswith("字"*300))
        for args in (("bad\nid","以后简短"),("x","字"*301),("x",""),("x","以后简短",1)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):source_text(*args)


class FallbackMemoryProposalTests(unittest.TestCase):
    def check_candidate(self, message, values):
        raw = fallback_memory_proposal(message)
        self.assertIsNotNone(raw, message)
        self.assertEqual(raw["values"], values)
        self.assertEqual(raw["scope"], "suggestion")
        self.assertEqual(raw["source_quote"], message.strip())
        self.assertEqual(validate_memory_proposal(raw, message)["status"], "proposed")

    def test_real_missed_feedback_is_only_offline_candidate_replay(self):
        # 真实调用曾漏提；这里只回放用户原文，不能当成新的真实模型成功证据。
        self.check_candidate("你刚才的回答太长了，请精简一点。", {"answer_style": "brief"})

    def test_brief_feedback_supports_multiple_phrasings(self):
        for message in ("太啰嗦了", "你的答复太啰嗦了", "回答请言简意赅。", "请精简一点", "解释简洁一些"):
            with self.subTest(message=message):
                self.check_candidate(message, {"answer_style": "brief"})

    def test_detailed_and_standard_feedback_are_distinguished(self):
        for message in ("回答太简略了", "解释请详细一些", "详细一点"):
            with self.subTest(message=message):
                self.check_candidate(message, {"answer_style": "detailed"})
        self.check_candidate("请恢复默认回答", {"answer_style": "standard"})

    def test_amount_feedback_requires_money_object_and_distinguishes_precision(self):
        cases = (("金额用元显示更直观", "yuan"), ("价格改成元显示", "yuan"),
                 ("金额用分显示", "fen"), ("金额精确到分", "yuan_fixed2"),
                 ("金额用元，保留两位小数", "yuan_fixed2"))
        for message, value in cases:
            with self.subTest(message=message):
                self.check_candidate(message, {"amount_format": value})

    def test_explicit_future_feedback_still_requires_confirmation_in_fallback(self):
        self.check_candidate("以后金额用元，回答简短", {"amount_format": "yuan", "answer_style": "brief"})

    def test_temporary_and_do_not_save_messages_do_not_generate_suggestions(self):
        for message in ("这次回答简短", "仅本轮金额用元", "本次你刚才的回答太长了，请精简一点", "不要保存偏好。回答简短。",
                        "临时改成详细回答", "以后回答简短。这次只是测试。"):
            with self.subTest(message=message):
                self.assertIsNone(fallback_memory_proposal(message))

    def test_third_party_quotes_code_and_external_content_are_not_user_feedback(self):
        for message in ("他说回答太长了", "他的回答太长了", "客户要求金额用元", "朋友觉得你的回答太啰嗦",
                        "“回答简短一点”", "`金额用元`", "```\n回答简短\n```", "> 回答简短", "手册规定回答简短",
                        "假如回答太长了", "例如金额用分显示", "翻译：回答简短"):
            with self.subTest(message=message):
                self.assertIsNone(fallback_memory_proposal(message))

    def test_negation_opposites_and_conflicting_values_do_not_guess(self):
        for message in ("回答不要简短", "回答不是太长", "回答没有那么啰嗦", "我不喜欢详细回答", "金额不要用分",
                        "金额用元也用分显示", "回答简短但是详细", "回答太简短了", "以后详细回答但不是偏好设置"):
            with self.subTest(message=message):
                self.assertIsNone(fallback_memory_proposal(message))

    def test_ambiguous_target_condition_question_and_identity_do_not_guess(self):
        for message in ("如果我忙，回答简短", "以后回答简短可以吗？", "买家回答简短", "账号B002金额用元", "请让回答像电报",
                        "订单号太长了", "回答里面的链接太长了", "物流周期太长了", "金额是多少", "金额125元", "简短的政策更好",
                        "用分段展示", "请用元", "今天天气真好", "这份文件附有详细说明", "详细说明是证据"):
            with self.subTest(message=message):
                self.assertIsNone(fallback_memory_proposal(message))

    def test_whole_source_is_never_truncated_or_coerced(self):
        for message in (None, 12, [], "", " " * 10, "回答简短" + "。" * 301, "回答简短\x00"):
            with self.subTest(message=message):
                self.assertIsNone(fallback_memory_proposal(message))


if __name__ == "__main__":
    unittest.main()

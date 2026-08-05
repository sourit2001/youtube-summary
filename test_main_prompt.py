import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


class SummaryPromptTests(unittest.TestCase):
    def test_summary_prompt_requires_natural_fact_driven_writing(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="【爆款标题】测试标题\n【正文内容】测试正文")
            )]
        )

        with patch.object(main, "OpenAI") as client_class:
            client_class.return_value.chat.completions.create.return_value = response
            main.summarize_content("测试字幕")

        prompt = client_class.return_value.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("如何发现用户需求", prompt)
        self.assertIn("正文长度控制在1000字以内", prompt)
        self.assertIn("每个段落必须带来一项新信息", prompt)
        self.assertIn("材料不足时宁可写短", prompt)
        self.assertIn("不得虚构字幕没有提供", prompt)
        self.assertIn("不在结尾复述全文或强行升华", prompt)
        self.assertIn("只输出最终文章，不展示检查过程", prompt)
        self.assertIn("不使用 Emoji", prompt)
        self.assertNotIn("丝滑的叙事感", prompt)
        self.assertNotIn("核心金句", prompt)


if __name__ == "__main__":
    unittest.main()

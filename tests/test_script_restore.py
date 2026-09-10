import unittest

from server.script_restore import restore_script


class ScriptRestoreTest(unittest.TestCase):
    def test_restores_repeated_reading(self):
        one = "。".join([
            "姐妹们今天这个石榴九块九",
            "到手一共六个",
            "这个籽特别软",
            "吃起来不用频繁吐籽",
            "现在下单按照顺序发货",
            "具体售后以订单页面为准",
        ]) + "。"
        text = one + one.replace("这个籽特别软", "咱这个软籽特别软") + one
        result = restore_script(text)
        self.assertTrue(result["detected"])
        self.assertGreaterEqual(len(result["rounds"]), 2)
        self.assertIn("九块九", result["standard_text"])
        self.assertLess(result["restored_units"], result["original_units"])

    def test_short_text_is_not_forced(self):
        result = restore_script("只有一句很短的话术。")
        self.assertFalse(result["detected"])
        self.assertEqual(result["standard_text"], "只有一句很短的话术。")


if __name__ == "__main__":
    unittest.main()

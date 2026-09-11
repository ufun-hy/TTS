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

    def test_two_complete_short_rounds(self):
        one = "产地云南。价格九块九。一单六个。果肉清甜。按顺序发货。售后联系客服。"
        result = restore_script(one * 2)
        self.assertTrue(result["detected"])
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(result["restored_units"], 6)

    def test_nonrepeating_text_is_not_removed(self):
        text = "云南果园直发。阳光充足。果子自然成熟。口感甜润。软籽容易吃。汁水充足。一箱六个。价格九块九。快递配送。按序发货。破损联系售后。订单页面查看信息。"
        result = restore_script(text)
        self.assertFalse(result["detected"])
        self.assertEqual(result["standard_text"], text)

    def test_short_text_is_not_forced(self):
        result = restore_script("只有一句很短的话术。")
        self.assertFalse(result["detected"])
        self.assertEqual(result["standard_text"], "只有一句很短的话术。")


if __name__ == "__main__":
    unittest.main()

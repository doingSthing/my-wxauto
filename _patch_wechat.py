import re

path = r"F:\ai-work\2026-4-28\my-wxauto\src\my_wxauto\wechat.py"
with open(path, "rb") as f:
    raw = f.read()

# 1. Add search_down_count and search_down_interval to SearchOptions
# Pattern: lines between restore_clipboard and class WeChat
old = b"restore_clipboard: bool = True\r\n\r\n"
idx = raw.find(old)
if idx < 0:
    print("ERROR: Cannot find restore_clipboard anchor")
    exit(1)
insert_point = idx + len(old) - 4  # before the \r\n\r\n between fields and class
insertion = b"    search_down_count: int = 1\r\n    search_down_interval: float = 0.06\r\n"
raw = raw[:insert_point] + insertion + raw[insert_point:]

# 2. Patch _open_chat_window: add Down presses before Enter
old2 = b"        time.sleep(wait_seconds)\r\n        keyboard.press(\"enter\")\r\n        time.sleep(self.search_options.chat_open_wait)"
new2 = b"""        time.sleep(wait_seconds)\r\n        down_count = self.search_options.search_down_count\r\n        for _ in range(down_count):\r\n            keyboard.press("down")\r\n            time.sleep(self.search_options.search_down_interval)\r\n        keyboard.press("enter")\r\n        time.sleep(self.search_options.chat_open_wait)"""
raw = raw.replace(old2, new2, 1)

# 3. Patch _search_from_focused_window: add Down presses before Enter
old3 = b"""        self.trace("shortcut_search.after_paste")\r\n        wait_seconds = float(force_wait) if force else self.search_options.result_wait\r\n        time.sleep(wait_seconds)\r\n        self.trace("shortcut_search.before_enter")\r\n        keyboard.press("enter")\r\n        self.trace("shortcut_search.after_enter")"""
new3 = b"""        self.trace("shortcut_search.after_paste")\r\n        wait_seconds = float(force_wait) if force else self.search_options.result_wait\r\n        time.sleep(wait_seconds)\r\n        down_count = self.search_options.search_down_count\r\n        for _ in range(down_count):\r\n            keyboard.press("down")\r\n            time.sleep(self.search_options.search_down_interval)\r\n        self.trace("shortcut_search.before_enter", down_count=down_count)\r\n        keyboard.press("enter")\r\n        self.trace("shortcut_search.after_enter")"""
raw = raw.replace(old3, new3, 1)

with open(path, "wb") as f:
    f.write(raw)
print("Done - all 3 patches applied")

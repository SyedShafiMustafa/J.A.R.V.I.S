from tools.desktop_control import DesktopController
from tools.computer import ComputerController
from tools.vision import ScreenVision
import time


class TaskExecutor:

    def __init__(self):
        self.desktop = DesktopController()
        self.computer = ComputerController()
        self.vision = ScreenVision()

    def execute(self, plan: dict):

        print(f"\n🎯 Goal: {plan['goal']}")

        for i, step in enumerate(plan["steps"], start=1):

            tool = step["tool"]
            print(f"⚙ Step {i}: {tool}")

            # ---------------- Desktop ----------------

            if tool == "open_app":
                self.desktop.open_app(step["app"])
                time.sleep(2)

            elif tool == "wait_window":
                found = self.computer.wait_for_window(step["title"])
                if found:
                    self.computer.focus_window(step["title"])
                else:
                    print(f"⚠ Window '{step['title']}' not found within timeout")
                time.sleep(0.8)

            elif tool == "close_app":
                self.desktop.close_app(step["app"])

            # ---------------- Browser ----------------

            elif tool == "open_youtube":
                self.desktop.open_youtube()
                time.sleep(2)

            elif tool == "search_youtube":
                self.desktop.search_youtube(step["query"])
                time.sleep(2)

            elif tool == "search_google":
                self.desktop.search_google(step["query"])
                time.sleep(2)

            # ---------------- Keyboard ----------------

            elif tool == "type":
                self.computer.type_text(step["text"])
                time.sleep(0.3)

            elif tool == "press":
                self.computer.press(step["key"])
                time.sleep(0.3)

            elif tool == "hotkey":
                self.computer.hotkey(*step["keys"])
                time.sleep(0.5)

            # ---------------- Semantic Vision ----------------

            elif tool == "click_text":

                success = self.vision.click_text(step["text"])

                if not success:
                    print(f"⚠ Could not find '{step['text']}' on screen — aborting task.")
                    return False

                time.sleep(0.5)

            else:
                print(f"Unknown tool: {tool}")
                return False

        print("\n✅ Task completed.")
        return True
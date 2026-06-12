"""Tiny tkinter window with continuous redraws.

Spawned as a subprocess by `conftest.py` for the duration of the test
session. Drives screen changes so DDA returns fresh frames during tests
WITHOUT touching the user's cursor (we used to call `user32.SetCursorPos`
which physically moved the mouse on the user's desktop -- never again).
"""
import sys
import tkinter as tk


def main():
    root = tk.Tk()
    root.title("rustcam-tests-stim")
    root.geometry("160x80+50+50")
    root.attributes("-topmost", True)
    root.configure(bg="black")

    canvas = tk.Canvas(root, width=160, height=80, bg="black", highlightthickness=0)
    canvas.pack()

    text = canvas.create_text(80, 40, text="0", fill="white",
                              font=("Courier", 22, "bold"))
    rect = canvas.create_rectangle(0, 0, 20, 20, fill="lime", outline="")

    state = {"n": 0, "x": 0}

    def tick():
        state["n"] += 1
        canvas.itemconfigure(text, text=f"{state['n']:05d}")
        state["x"] = (state["x"] + 5) % 140
        canvas.coords(rect, state["x"], 0, state["x"] + 20, 20)
        root.after(1, tick)

    root.after(1, tick)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())

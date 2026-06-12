"""Background stimulus window that updates its content every tick.

Spawned as a subprocess by the benchmark to give the screen pixels that
visibly change at ~refresh rate, so DDA captures see real uniques (not
the stale-cache trap where libraries that return last-buffer look fast).

Uses tkinter (stdlib) so no GUI deps required.
"""
import time
import tkinter as tk


def main():
    root = tk.Tk()
    root.title("rustcam-stim")
    root.geometry("400x240+100+100")
    root.configure(bg="black")
    root.attributes("-topmost", True)

    canvas = tk.Canvas(root, width=400, height=240, bg="black", highlightthickness=0)
    canvas.pack()

    text = canvas.create_text(
        200, 120, text="0", fill="white", font=("Courier", 48, "bold")
    )
    rect = canvas.create_rectangle(0, 0, 40, 40, fill="lime", outline="")

    state = {"n": 0, "x": 0, "y": 0}

    def tick():
        state["n"] += 1
        canvas.itemconfigure(text, text=f"{state['n']:08d}")
        state["x"] = (state["x"] + 7) % 360
        state["y"] = (state["y"] + 5) % 200
        canvas.coords(rect, state["x"], state["y"], state["x"] + 40, state["y"] + 40)
        root.after(1, tick)

    root.after(1, tick)
    root.mainloop()


if __name__ == "__main__":
    main()

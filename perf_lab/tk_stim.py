"""Like tests/_stim_window.py but takes SECS arg and a bigger canvas
with more visual churn (so DWM has more compositing work).
"""
import sys
import tkinter as tk
import time

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0


def main():
    root = tk.Tk()
    root.title("rustcam-perf-tk-stim")
    root.geometry("400x300+200+150")
    root.attributes("-topmost", True)
    root.configure(bg="black")

    canvas = tk.Canvas(root, width=400, height=300, bg="black",
                       highlightthickness=0)
    canvas.pack()

    text = canvas.create_text(200, 150, text="0", fill="white",
                              font=("Courier", 32, "bold"))
    rect = canvas.create_rectangle(0, 0, 40, 40, fill="lime", outline="")
    rect2 = canvas.create_rectangle(0, 260, 40, 300, fill="cyan", outline="")

    state = {"n": 0, "x": 0, "x2": 360, "t0": time.perf_counter()}

    def tick():
        state["n"] += 1
        canvas.itemconfigure(text, text=f"{state['n']:05d}")
        state["x"] = (state["x"] + 7) % 360
        state["x2"] = (state["x2"] - 5) % 360
        canvas.coords(rect, state["x"], 0, state["x"] + 40, 40)
        canvas.coords(rect2, state["x2"], 260, state["x2"] + 40, 300)
        if time.perf_counter() - state["t0"] > SECS:
            root.destroy()
            return
        root.after(1, tick)

    root.after(1, tick)
    root.mainloop()


if __name__ == "__main__":
    sys.exit(main())

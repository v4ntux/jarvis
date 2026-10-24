"""Local screen parser for Jarvis's hands: OmniParser V2 on the laptop's GPU.

Screenshot in, elements out as text. YOLO finds icons and buttons, EasyOCR reads the
words, Florence-2 names the icons that have no words. Nothing leaves the laptop and
nothing costs money: the brain gets a short list instead of a picture.

Runs in its own environment, started on demand by hands.py:
    .vision-venv\\Scripts\\python.exe vision\\parse_server.py
POST http://127.0.0.1:18766/parse  {"image": "<base64 PNG>"}  ->  {"elements": [...], "ms": 1234}
GET  http://127.0.0.1:18766/health ->  {"ok": true, "device": "cuda"}
"""
import base64
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import torch
from PIL import Image

MODELS = Path(__file__).resolve().parent / "models"
PORT = 18766
IDLE_EXIT = 20 * 60  # frees the GPU memory when nobody has looked for a while
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.float16 if DEVICE == "cuda" else torch.float32
BOX_THRESHOLD = 0.05
OCR_MIN_CONFIDENCE = 0.3
CAPTION_BATCH = 32
CROP = 64
MAX_ELEMENTS = 150


def _load():
    from ultralytics import YOLO
    import easyocr
    from transformers import AutoProcessor

    detector = YOLO(str(MODELS / "icon_detect" / "model.pt"))
    reader = easyocr.Reader(["ru", "en"], gpu=DEVICE == "cuda", verbose=False,
                            model_storage_directory=str(MODELS / "easyocr"),
                            user_network_directory=str(MODELS / "easyocr"))
    processor = AutoProcessor.from_pretrained(str(MODELS / "florence_native"))
    # Icons are captioned at 64x64 like OmniParser does, not stretched to 768: DaViT turns that
    # into (64/32)^2 patches plus one pooled token, and the prompt must hold as many placeholders
    processor.num_image_tokens = (CROP // 32) ** 2 + 1
    captioner = _load_captioner()
    return detector, reader, processor, captioner


def _load_captioner():
    from transformers import Florence2ForConditionalGeneration

    # Converted once from OmniParser's weights by convert_caption.py: no hub code is ever run
    model = Florence2ForConditionalGeneration.from_pretrained(str(MODELS / "icon_caption_hf"), dtype=DTYPE)
    return model.to(DEVICE).eval()


def _overlap(inner, outer):
    """Share of inner's area that lies inside outer."""
    left, top = max(inner[0], outer[0]), max(inner[1], outer[1])
    right, bottom = min(inner[2], outer[2]), min(inner[3], outer[3])
    if right <= left or bottom <= top:
        return 0.0
    area = max(1.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return (right - left) * (bottom - top) / area


class Parser:
    def __init__(self):
        self.detector, self.reader, self.processor, self.captioner = _load()
        self.lock = threading.Lock()  # one GPU, one picture at a time

    def _texts(self, image):
        found = []
        for points, text, confidence in self.reader.readtext(np.array(image), paragraph=False):
            text = str(text).strip()
            if confidence < OCR_MIN_CONFIDENCE or not text:
                continue
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            found.append({"box": [min(xs), min(ys), max(xs), max(ys)], "content": text})
        return found

    def _icons(self, image):
        result = self.detector.predict(image, conf=BOX_THRESHOLD, iou=0.1, verbose=False, device=DEVICE)[0]
        return [[float(v) for v in box] for box in result.boxes.xyxy.cpu().tolist()]

    def _captions(self, image, boxes):
        names = []
        for start in range(0, len(boxes), CAPTION_BATCH):
            crops = [image.crop(tuple(int(v) for v in box)).convert("RGB").resize((CROP, CROP))
                     for box in boxes[start:start + CAPTION_BATCH]]
            inputs = self.processor(images=crops, text=["<CAPTION>"] * len(crops), return_tensors="pt",
                                    do_resize=False)
            inputs = {k: (v.to(DEVICE, DTYPE) if v.dtype.is_floating_point else v.to(DEVICE)) for k, v in inputs.items()}
            with torch.inference_mode():
                ids = self.captioner.generate(input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                                              max_new_tokens=20, num_beams=1, do_sample=False)
            names += [t.strip() for t in self.processor.batch_decode(ids, skip_special_tokens=True)]
        return names

    def parse(self, image):
        with self.lock:
            texts = self._texts(image)
            icons = self._icons(image)
            elements, used, unnamed = [], set(), []
            for box in icons:
                inside = [i for i, t in enumerate(texts) if i not in used and _overlap(t["box"], box) >= 0.8]
                if inside:
                    used.update(inside)
                    label = " ".join(texts[i]["content"] for i in sorted(inside, key=lambda i: texts[i]["box"][0]))
                    elements.append({"type": "button", "box": box, "content": label})
                else:
                    unnamed.append(box)
            for box, caption in zip(unnamed, self._captions(image, unnamed) if unnamed else []):
                elements.append({"type": "icon", "box": box, "content": caption})
            elements += [{"type": "text", **t} for i, t in enumerate(texts) if i not in used]
        elements.sort(key=lambda e: (round(e["box"][1] / 12), e["box"][0]))
        for n, e in enumerate(elements[:MAX_ELEMENTS], 1):
            e["id"] = "v%d" % n
            e["box"] = [int(v) for v in e["box"]]
            e["center"] = [(e["box"][0] + e["box"][2]) // 2, (e["box"][1] + e["box"][3]) // 2]
        return elements[:MAX_ELEMENTS]


class Handler(BaseHTTPRequestHandler):
    parser = None
    last_use = time.monotonic()

    def _reply(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "device": DEVICE})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/parse":
            self._reply(404, {"error": "not found"})
            return
        Handler.last_use = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length))
            image = Image.open(io.BytesIO(base64.b64decode(data["image"]))).convert("RGB")
            started = time.monotonic()
            elements = self.parser.parse(image)
            self._reply(200, {"elements": elements, "ms": int((time.monotonic() - started) * 1000),
                              "size": list(image.size)})
        except Exception as exc:
            self._reply(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
        Handler.last_use = time.monotonic()

    def log_message(self, *args):
        pass


def _exit_when_idle(server):
    while True:
        time.sleep(30)
        if time.monotonic() - Handler.last_use > IDLE_EXIT:
            server.shutdown()
            return


def main():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # every model is already on disk
    Handler.parser = Parser()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=_exit_when_idle, args=(server,), daemon=True).start()
    print("vision ready on %s" % DEVICE, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    if "--check" in sys.argv:
        parser = Parser()
        image = Image.open(sys.argv[sys.argv.index("--check") + 1]).convert("RGB")
        started = time.monotonic()
        out = parser.parse(image)
        print("%d elements in %d ms on %s" % (len(out), (time.monotonic() - started) * 1000, DEVICE))
        for e in out[:40]:
            print(e["id"], e["type"], repr(e["content"])[:60], e["center"])
    else:
        main()

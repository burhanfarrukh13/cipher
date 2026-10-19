"""
Face recognition skill — offline "who is this" check using OpenCV's
Haar-cascade face detector + LBPH recognizer. Same well-known local
technique used by a lot of DIY assistant projects (including the
J.A.R.V.I.S repo you linked): detect a face, crop it, train an LBPH
model per person, then match new frames against it. No cloud API,
no external model download — the cascade file ships with opencv
itself (`cv2.data.haarcascades`).

Needs opencv-contrib-python (not plain opencv-python) for cv2.face.*:
    pip install opencv-contrib-python numpy

Flow:
    1. enroll(name)  — capture ~25 face photos from the webcam
    2. train()       — build/rebuild the LBPH model from everyone enrolled
    3. verify()      — grab a frame, say who (if anyone) it matches

Data layout, all under backend/data/faces/:
    raw/<label_id>/*.jpg   — captured training photos per person
    labels.json            — {"1": "Alex", "2": "Sam", ...}
    trainer.yml            — trained LBPH model
"""
import json
import time
from pathlib import Path

import cv2
import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "faces"
RAW_DIR = DATA_DIR / "raw"
LABELS_FILE = DATA_DIR / "labels.json"
TRAINER_FILE = DATA_DIR / "trainer.yml"

DATA_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

# Ships with opencv itself — no separate download needed.
CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"

# LBPH reports a *distance* (lower = more confident match). This is a
# reasonable starting point for a webcam in normal indoor lighting —
# tune it down if it's accepting strangers, up if it's rejecting you.
CONFIDENCE_THRESHOLD = 70


def _load_labels() -> dict:
    if not LABELS_FILE.exists():
        return {}
    return json.loads(LABELS_FILE.read_text())


def _save_labels(labels: dict) -> None:
    LABELS_FILE.write_text(json.dumps(labels, indent=2))


def is_trained() -> bool:
    return TRAINER_FILE.exists() and LABELS_FILE.exists()


def enrolled_names() -> list[str]:
    return sorted(_load_labels().values())


def enroll(name: str, num_samples: int = 25, camera_index: int = 0) -> dict:
    """
    Opens the webcam and saves `num_samples` cropped grayscale face
    images for `name`. Blocking (takes a few seconds). Call train()
    afterwards to actually update the recognizer.
    """
    name = (name or "").strip()
    if not name:
        return {"error": "Name can't be empty."}

    labels = _load_labels()
    existing_id = next((int(k) for k, v in labels.items() if v.lower() == name.lower()), None)
    label_id = existing_id if existing_id is not None else (max([int(k) for k in labels], default=0) + 1)
    labels[str(label_id)] = name
    _save_labels(labels)

    person_dir = RAW_DIR / str(label_id)
    person_dir.mkdir(parents=True, exist_ok=True)
    start_index = len(list(person_dir.glob("*.jpg")))

    detector = cv2.CascadeClassifier(CASCADE_PATH)
    cam = cv2.VideoCapture(camera_index)
    if not cam.isOpened():
        return {"error": f"Couldn't open camera index {camera_index}. Is it in use by another app?"}

    collected = 0
    deadline = time.time() + 20  # don't hang forever if no face is ever in frame
    try:
        while collected < num_samples and time.time() < deadline:
            ok, frame = cam.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5, minSize=(80, 80))
            for (x, y, w, h) in faces:
                face_img = gray[y:y + h, x:x + w]
                out_path = person_dir / f"{start_index + collected}.jpg"
                cv2.imwrite(str(out_path), face_img)
                collected += 1
                break  # one sample per frame is plenty
    finally:
        cam.release()

    if collected == 0:
        return {"error": "No face detected — check the camera and lighting, then try again."}

    return {"ok": True, "name": name, "label_id": label_id, "samples_collected": collected}


def train() -> dict:
    """Rebuilds the LBPH model from everything currently under raw/."""
    labels = _load_labels()
    if not labels:
        return {"error": "No one enrolled yet — call enroll() first."}

    recognizer = cv2.face.LBPHFaceRecognizer_create()
    faces, ids = [], []
    for label_id_str in labels:
        person_dir = RAW_DIR / label_id_str
        for img_path in person_dir.glob("*.jpg"):
            img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                faces.append(img)
                ids.append(int(label_id_str))

    if not faces:
        return {"error": "No training images found on disk under data/faces/raw/."}

    recognizer.train(faces, np.array(ids))
    recognizer.write(str(TRAINER_FILE))
    return {"ok": True, "people": enrolled_names(), "samples_used": len(faces)}


def verify(camera_index: int = 0, timeout_sec: int = 6) -> dict:
    """
    Scans the webcam for up to `timeout_sec` seconds and returns the
    best-confidence match against the trained model, or
    recognized: False if no one matched closely enough — or if the scan
    didn't complete for any reason (camera error, exception, etc). This
    always fails CLOSED: any path that isn't an explicit confident match
    returns recognized: False, never leaves it ambiguous.
    """
    if not is_trained():
        return {"recognized": False, "error": "No trained model yet. Enroll someone and call train() first."}

    cam = None
    try:
        labels = _load_labels()
        recognizer = cv2.face.LBPHFaceRecognizer_create()
        recognizer.read(str(TRAINER_FILE))
        detector = cv2.CascadeClassifier(CASCADE_PATH)

        cam = cv2.VideoCapture(camera_index)
        if not cam.isOpened():
            return {"recognized": False, "error": f"Couldn't open camera index {camera_index}. Is it in use by another app?"}

        best_name, best_confidence = None, None
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            ok, frame = cam.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5, minSize=(80, 80))
            for (x, y, w, h) in faces:
                face_img = gray[y:y + h, x:x + w]
                label_id, confidence = recognizer.predict(face_img)  # lower = closer match
                if best_confidence is None or confidence < best_confidence:
                    best_confidence = confidence
                    best_name = labels.get(str(label_id), "unknown")
            if best_confidence is not None and best_confidence < CONFIDENCE_THRESHOLD:
                break  # good enough match, stop scanning early

        if best_name is None:
            # Scan ran to completion but never saw a face at all — this is
            # the "didn't complete" case. Stay locked, be explicit about why.
            return {"recognized": False, "reason": "No face detected in frame — scan didn't complete. Try again with better lighting/angle."}

        recognized = best_confidence < CONFIDENCE_THRESHOLD
        return {
            "recognized": recognized,
            "name": best_name if recognized else None,
            "confidence": round(float(best_confidence), 1),
        }
    except Exception as e:
        # Any unexpected camera/recognition failure must never be treated
        # as a pass — fail closed and stay locked, always return a usable dict.
        return {"recognized": False, "error": f"Face verification didn't complete: {e}"}
    finally:
        if cam is not None:
            cam.release()

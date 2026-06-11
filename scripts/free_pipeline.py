#!/usr/bin/env python3
"""
Free Translation Pipeline — My Translator
==========================================
Hoàn toàn miễn phí, không cần API key.
Chạy được trên Windows, macOS (Intel + Apple Silicon), Linux.

Pipeline:
  System Audio (PCM s16le 16kHz mono via stdin)
    → faster-whisper (STT, chạy local CPU/GPU)
    → MyMemory API hoặc LibreTranslate (dịch, free không key)
    → JSON output via stdout

Cài đặt (một lần duy nhất):
  pip install faster-whisper requests numpy

Sử dụng:
  python3 free_pipeline.py --source-lang ja --target-lang vi --whisper-model small
  python3 free_pipeline.py --source-lang en --target-lang vi --whisper-model tiny
"""

import sys
import os
import json
import time
import wave
import tempfile
import threading
import argparse
import numpy as np
import urllib.request
import urllib.parse
import urllib.error

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# ─── Whisper model sizes vs chất lượng/tốc độ ───────────────────────────────
# tiny   : ~75MB  — rất nhanh, chất lượng thấp
# base   : ~145MB — nhanh, chất lượng khá
# small  : ~466MB — cân bằng tốt (khuyến nghị)
# medium : ~1.5GB — chất lượng cao, cần RAM tốt
# large  : ~2.9GB — tốt nhất, cần GPU/RAM mạnh
# ─────────────────────────────────────────────────────────────────────────────

LANG_NAMES = {
    "vi": "Vietnamese", "en": "English", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "fr": "French",
    "de": "German", "es": "Spanish", "th": "Thai",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "ar": "Arabic", "id": "Indonesian", "ms": "Malay",
}

# MyMemory language codes (khác với Whisper codes ở một số ngôn ngữ)
MYMEMORY_LANG = {
    "vi": "vi-VN", "en": "en-US", "ja": "ja-JP",
    "ko": "ko-KR", "zh": "zh-CN", "fr": "fr-FR",
    "de": "de-DE", "es": "es-ES", "it": "it-IT",
    "pt": "pt-PT", "ru": "ru-RU", "th": "th-TH",
    "id": "id-ID", "ms": "ms-MY", "ar": "ar-SA",
}


def log(msg):
    print(f"[free-pipeline] {msg}", file=sys.stderr, flush=True)


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


# ─── Translation backends ─────────────────────────────────────────────────────

def translate_mymemory(text, source_lang, target_lang):
    """
    MyMemory API — hoàn toàn free, không cần key.
    Giới hạn: 5000 ký tự/ngày (không key) / 50.000 (có email).
    Phù hợp với dịch realtime câu ngắn.
    """
    src = MYMEMORY_LANG.get(source_lang, source_lang)
    tgt = MYMEMORY_LANG.get(target_lang, target_lang)
    lang_pair = f"{src}|{tgt}"
    url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(text)}&langpair={urllib.parse.quote(lang_pair)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MyTranslator/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        if data.get("responseStatus") == 200:
            result = data["responseData"]["translatedText"]
            # MyMemory đôi khi trả về lỗi dạng text
            if result and not result.startswith("MYMEMORY WARNING"):
                return result.strip()
    except Exception as e:
        log(f"MyMemory error: {e}")
    return None


def translate_libretranslate(text, source_lang, target_lang, host="https://libretranslate.com"):
    """
    LibreTranslate — open source, có thể tự host hoặc dùng public instance.
    Các public server miễn phí: libretranslate.com, translate.argosopentech.com
    """
    url = f"{host}/translate"
    payload = json.dumps({
        "q": text,
        "source": source_lang if source_lang != "auto" else "auto",
        "target": target_lang,
        "format": "text",
    }).encode()
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "MyTranslator/1.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        return data.get("translatedText", "").strip()
    except Exception as e:
        log(f"LibreTranslate ({host}) error: {e}")
    return None


def translate_argos(text, source_lang, target_lang):
    """Argos Translate public instance — free, no key."""
    return translate_libretranslate(
        text, source_lang, target_lang,
        host="https://translate.argosopentech.com"
    )


def translate_with_fallback(text, source_lang, target_lang):
    """
    Thử các backend theo thứ tự ưu tiên:
    1. MyMemory (nhanh nhất, free)
    2. LibreTranslate public
    3. Argos public
    Nếu tất cả fail, trả về text gốc.
    """
    if not text or not text.strip():
        return ""

    # Backend 1: MyMemory
    result = translate_mymemory(text, source_lang, target_lang)
    if result:
        log(f"[MyMemory] {text[:40]} → {result[:40]}")
        return result

    # Backend 2: LibreTranslate
    result = translate_libretranslate(text, source_lang, target_lang)
    if result:
        log(f"[LibreTranslate] {text[:40]} → {result[:40]}")
        return result

    # Backend 3: Argos
    result = translate_argos(text, source_lang, target_lang)
    if result:
        log(f"[Argos] {text[:40]} → {result[:40]}")
        return result

    log(f"All translation backends failed for: {text[:60]}")
    return f"[{text}]"  # Trả về nguyên bản có dấu ngoặc để người dùng biết chưa dịch được


# ─── Main Pipeline ────────────────────────────────────────────────────────────

class FreePipeline:
    def __init__(
        self,
        source_lang="ja",
        target_lang="vi",
        whisper_model="small",
        chunk_seconds=6,
        stride_seconds=4,
        device="auto",
    ):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.whisper_model_size = whisper_model
        self.chunk_seconds = chunk_seconds
        self.stride_seconds = stride_seconds
        self.sample_rate = 16000
        self.bytes_per_sample = 2

        self.chunk_bytes = chunk_seconds * self.sample_rate * self.bytes_per_sample
        self.stride_bytes = stride_seconds * self.sample_rate * self.bytes_per_sample

        self.audio_buffer = bytearray()
        self.lock = threading.Lock()
        self.running = True
        self.prev_text = ""
        self.model = None
        self.device = device

        self._load_model()

    def _load_model(self):
        """Load faster-whisper model."""
        log(f"Loading faster-whisper [{self.whisper_model_size}]...")
        emit({"type": "status", "message": f"Đang tải Whisper ({self.whisper_model_size})..."})

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            emit({"type": "error", "message": "Thiếu thư viện: chạy 'pip install faster-whisper' rồi thử lại."})
            sys.exit(1)

        t = time.time()

        # Tự chọn device: dùng CUDA nếu có, fallback về CPU
        if self.device == "auto":
            try:
                import torch
                if torch.cuda.is_available():
                    device = "cuda"
                    compute_type = "float16"
                    log("Using CUDA GPU")
                else:
                    device = "cpu"
                    compute_type = "int8"
                    log("Using CPU (no CUDA found)")
            except ImportError:
                device = "cpu"
                compute_type = "int8"
                log("Using CPU (torch not installed)")
        elif self.device == "cuda":
            device = "cuda"
            compute_type = "float16"
        else:
            device = "cpu"
            compute_type = "int8"

        self.model = WhisperModel(
            self.whisper_model_size,
            device=device,
            compute_type=compute_type,
            download_root=os.path.join(os.path.expanduser("~"), ".cache", "my-translator", "whisper"),
        )
        log(f"Whisper loaded in {time.time()-t:.1f}s on {device}")

        # Warmup
        emit({"type": "status", "message": "Khởi động Whisper..."})
        dummy = np.zeros(1600, dtype=np.float32)
        list(self.model.transcribe(dummy, language=self._whisper_lang()))
        log("Whisper warmed up")

        # Test translation backends
        emit({"type": "status", "message": "Kiểm tra kết nối dịch thuật..."})
        test = translate_with_fallback("Hello", "en", self.target_lang)
        log(f"Translation test: Hello → {test}")

        emit({"type": "ready"})
        log("Free pipeline ready!")

    def _whisper_lang(self):
        """Trả về mã ngôn ngữ Whisper, hoặc None nếu auto."""
        if self.source_lang == "auto":
            return None
        lang_map = {
            "ja": "ja", "en": "en", "zh": "zh", "ko": "ko",
            "vi": "vi", "fr": "fr", "de": "de", "es": "es",
            "th": "th", "it": "it", "pt": "pt", "ru": "ru",
            "id": "id", "ms": "ms",
        }
        return lang_map.get(self.source_lang, self.source_lang)

    def _transcribe(self, pcm_bytes):
        """Chuyển PCM bytes → text bằng faster-whisper."""
        # Convert PCM s16le → float32 normalized
        audio_np = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        lang = self._whisper_lang()
        kwargs = {"beam_size": 3, "language": lang, "task": "transcribe", "vad_filter": True}
        # VAD filter tự động loại bỏ đoạn im lặng — rất quan trọng cho realtime

        segments, info = self.model.transcribe(audio_np, **kwargs)
        text = " ".join(seg.text for seg in segments).strip()
        detected_lang = info.language if info else (lang or self.source_lang)
        return text, detected_lang

    def _dedup(self, text):
        """Loại bỏ phần trùng lặp với chunk trước."""
        if not self.prev_text or not text:
            return text
        prev = self.prev_text
        best = 0
        min_overlap = 4
        max_check = min(len(prev), len(text), 120)
        for length in range(min_overlap, max_check + 1):
            if prev[-length:] == text[:length]:
                best = length
        if best >= min_overlap:
            new = text[best:].strip()
            return new if new else text
        return text

    def _process_chunk(self, pcm_bytes):
        """Xử lý một chunk: transcribe → dịch → emit."""
        t_start = time.time()

        # Kiểm tra có âm thanh thực sự không
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2))
        if rms < 80:
            return  # Im lặng, bỏ qua

        try:
            # 1. Transcribe
            t1 = time.time()
            text, lang = self._transcribe(pcm_bytes)
            t_asr = time.time() - t1

            if not text or text == self.prev_text:
                return

            new_text = self._dedup(text)
            if not new_text or len(new_text) < 2:
                self.prev_text = text
                return

            log(f"ASR ({t_asr:.2f}s): {new_text[:60]}")

            # 2. Dịch
            t2 = time.time()
            translated = translate_with_fallback(new_text, lang or self.source_lang, self.target_lang)
            t_trans = time.time() - t2

            log(f"Trans ({t_trans:.2f}s): {translated[:60]}")

            # 3. Emit
            emit({
                "type": "result",
                "original": new_text,
                "translated": translated,
                "language": lang or self.source_lang,
                "timing": {
                    "asr": round(t_asr, 2),
                    "translate": round(t_trans, 2),
                    "total": round(time.time() - t_start, 2),
                },
            })

            self.prev_text = text

        except Exception as e:
            log(f"Chunk error: {e}")
            emit({"type": "error", "message": f"Lỗi xử lý audio: {e}"})

    def stdin_reader(self):
        """Đọc PCM bytes từ stdin liên tục."""
        try:
            while self.running:
                data = sys.stdin.buffer.read(4096)
                if not data:
                    break
                with self.lock:
                    self.audio_buffer.extend(data)
        except Exception as e:
            log(f"stdin reader: {e}")
        finally:
            self.running = False

    def run(self):
        """Vòng lặp chính."""
        reader = threading.Thread(target=self.stdin_reader, daemon=True)
        reader.start()

        processed_pos = 0

        while self.running:
            time.sleep(0.3)
            with self.lock:
                buf_len = len(self.audio_buffer)

            if buf_len - processed_pos >= self.chunk_bytes:
                with self.lock:
                    chunk = bytes(self.audio_buffer[processed_pos: processed_pos + self.chunk_bytes])
                self._process_chunk(chunk)
                processed_pos += self.stride_bytes

        # Xử lý phần còn lại
        with self.lock:
            remaining = bytes(self.audio_buffer[processed_pos:])
        if len(remaining) > self.sample_rate * self.bytes_per_sample:
            self._process_chunk(remaining)

        emit({"type": "done"})
        log("Pipeline stopped.")


def check_dependencies():
    """Kiểm tra các thư viện cần thiết."""
    missing = []
    try:
        import faster_whisper
    except ImportError:
        missing.append("faster-whisper")
    try:
        import numpy
    except ImportError:
        missing.append("numpy")
    if missing:
        emit({
            "type": "error",
            "message": f"Thiếu thư viện: {', '.join(missing)}. Chạy: pip install {' '.join(missing)}"
        })
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Free Translation Pipeline")
    parser.add_argument("--source-lang", default="ja",
                        help="Ngôn ngữ nguồn (ja, en, ko, zh, auto, ...)")
    parser.add_argument("--target-lang", default="vi",
                        help="Ngôn ngữ đích (vi, en, ...)")
    parser.add_argument("--whisper-model", default="small",
                        choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"],
                        help="Model Whisper: tiny/base/small/medium/large-v3")
    parser.add_argument("--chunk-seconds", type=int, default=6,
                        help="Kích thước chunk audio (giây)")
    parser.add_argument("--stride-seconds", type=int, default=4,
                        help="Bước trượt giữa các chunk (giây)")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"],
                        help="Thiết bị: auto/cpu/cuda")
    parser.add_argument("--translate-only", type=str,
                        help="Chỉ dịch một câu rồi thoát (để test)")
    args = parser.parse_args()

    if args.translate_only:
        # Mode test dịch nhanh
        result = translate_with_fallback(args.translate_only, args.source_lang, args.target_lang)
        print(f"Input:  {args.translate_only}")
        print(f"Output: {result}")
        return

    check_dependencies()

    pipeline = FreePipeline(
        source_lang=args.source_lang,
        target_lang=args.target_lang,
        whisper_model=args.whisper_model,
        chunk_seconds=args.chunk_seconds,
        stride_seconds=args.stride_seconds,
        device=args.device,
    )
    pipeline.run()


if __name__ == "__main__":
    main()

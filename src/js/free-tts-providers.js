/**
 * Free TTS Providers — No API key required
 * Includes:
 *   1. Web Speech API  — Browser built-in, uses system voices
 *   2. Google Translate TTS  — Unofficial free endpoint
 *   3. Kokoro TTS (browser ONNX) — High quality offline model
 */

// ─────────────────────────────────────────────────────────────────────────────
// 1. Web Speech TTS — Browser built-in SpeechSynthesis
// ─────────────────────────────────────────────────────────────────────────────
class WebSpeechTTSProvider {
    constructor() {
        this.voiceName = null;
        this.rate = 1.1;
        this.pitch = 1.0;
        this.volume = 1.0;
        this.lang = 'vi-VN';
        this.isConnected = false;
        this._voice = null;

        this.onAudioChunk = null;
        this.onError = null;
        this.onStatusChange = null;
    }

    configure({ voiceName, lang, rate, pitch }) {
        if (voiceName !== undefined) this.voiceName = voiceName;
        if (lang) this.lang = lang;
        if (rate !== undefined) this.rate = rate;
        if (pitch !== undefined) this.pitch = pitch;
        this._resolveVoice();
    }

    _resolveVoice() {
        const voices = speechSynthesis.getVoices();
        if (voices.length === 0) {
            speechSynthesis.addEventListener('voiceschanged', () => this._resolveVoice(), { once: true });
            return;
        }
        if (this.voiceName) {
            this._voice = voices.find(v => v.name === this.voiceName) || null;
        }
        if (!this._voice) {
            this._voice = voices.find(v => v.lang === this.lang)
                || voices.find(v => v.lang.startsWith(this.lang.split('-')[0]))
                || null;
        }
        console.log('[WebSpeech] Voice:', this._voice?.name || '(system default)');
    }

    connect() {
        this._resolveVoice();
        this.isConnected = true;
        this._setStatus('connected');
    }

    speak(text) {
        if (!text?.trim()) return;
        const utt = new SpeechSynthesisUtterance(text.trim());
        utt.lang = this.lang;
        utt.rate = this.rate;
        utt.pitch = this.pitch;
        utt.volume = this.volume;
        if (this._voice) utt.voice = this._voice;
        utt.onerror = (e) => {
            if (e.error !== 'canceled') this.onError?.(`WebSpeech: ${e.error}`);
        };
        speechSynthesis.speak(utt);
    }

    disconnect() {
        speechSynthesis.cancel();
        this.isConnected = false;
        this._setStatus('disconnected');
    }

    _setStatus(s) { this.onStatusChange?.(s); }

    /** Return all available system voices grouped by language */
    static getVoices() {
        return speechSynthesis.getVoices();
    }

    static getVoicesForLang(langPrefix) {
        return speechSynthesis.getVoices().filter(v => v.lang.startsWith(langPrefix));
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 2. Google Translate TTS — Unofficial free endpoint (no key needed)
//    Uses the same API that Google Translate web uses.
//    Limit: ~200 chars per request (auto-splits longer text).
//    Note: Not officially supported by Google; may break. Works well for short
//    translated sentences which is exactly our use case.
// ─────────────────────────────────────────────────────────────────────────────

// Language code mapping: our internal lang code → Google Translate lang code
const GTRANSLATE_LANG_MAP = {
    'vi': 'vi', 'en': 'en', 'ja': 'ja', 'ko': 'ko',
    'zh': 'zh-CN', 'zh-TW': 'zh-TW', 'fr': 'fr', 'de': 'de',
    'es': 'es', 'it': 'it', 'pt': 'pt', 'ru': 'ru',
    'ar': 'ar', 'th': 'th', 'id': 'id', 'ms': 'ms',
};

class GoogleTranslateTTSProvider {
    constructor() {
        this.lang = 'vi';
        this.speed = 1.0;   // 0.5 = slow, 1.0 = normal
        this.isConnected = false;
        this._queue = [];
        this._isSpeaking = false;

        this.onAudioChunk = null;
        this.onError = null;
        this.onStatusChange = null;
    }

    configure({ lang, speed }) {
        if (lang) this.lang = GTRANSLATE_LANG_MAP[lang] || lang;
        if (speed !== undefined) this.speed = speed;
    }

    connect() {
        this.isConnected = true;
        this._setStatus('connected');
        console.log('[GTranslate TTS] Ready (free, no key)');
    }

    speak(text) {
        if (!text?.trim()) return;
        // Split at ~180 chars on sentence boundaries to stay within limit
        const chunks = this._splitText(text.trim(), 180);
        chunks.forEach(c => this._queue.push(c));
        if (!this._isSpeaking) this._processQueue();
    }

    _splitText(text, maxLen) {
        if (text.length <= maxLen) return [text];
        const result = [];
        let remaining = text;
        while (remaining.length > maxLen) {
            // Try to break at sentence end
            let cut = remaining.lastIndexOf('. ', maxLen);
            if (cut < 60) cut = remaining.lastIndexOf(' ', maxLen);
            if (cut < 10) cut = maxLen;
            result.push(remaining.slice(0, cut + 1).trim());
            remaining = remaining.slice(cut + 1).trim();
        }
        if (remaining) result.push(remaining);
        return result;
    }

    async _processQueue() {
        if (this._queue.length === 0) { this._isSpeaking = false; return; }
        this._isSpeaking = true;
        const text = this._queue.shift();
        try {
            // Google Translate TTS endpoint (client=tw-ob bypasses some restrictions)
            const url = `https://translate.google.com/translate_tts?ie=UTF-8&q=${encodeURIComponent(text)}&tl=${encodeURIComponent(this.lang)}&total=1&idx=0&textlen=${text.length}&client=tw-ob`;
            const resp = await fetch(url);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const blob = await resp.blob();
            const arrayBuffer = await blob.arrayBuffer();
            const base64 = btoa(String.fromCharCode(...new Uint8Array(arrayBuffer)));
            if (this.onAudioChunk) this.onAudioChunk(base64, true);
        } catch (err) {
            console.error('[GTranslate TTS] Error:', err);
            this.onError?.(`Google Translate TTS: ${err.message}`);
        }
        this._processQueue();
    }

    disconnect() {
        this._queue = [];
        this._isSpeaking = false;
        this.isConnected = false;
        this._setStatus('disconnected');
    }

    _setStatus(s) { this.onStatusChange?.(s); }
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. Microsoft Azure TTS Free Tier — 500,000 chars/month free
//    Requires a free Azure account (no credit card needed for F0 tier).
//    Uses standard voices which sound very natural.
//    Endpoint: eastus.tts.speech.microsoft.com (or chosen region)
// ─────────────────────────────────────────────────────────────────────────────

const AZURE_VOICE_MAP = {
    'vi-VN': [
        { name: 'vi-VN-HoaiMyNeural', label: 'HoaiMy — Nữ 🎀' },
        { name: 'vi-VN-NamMinhNeural', label: 'NamMinh — Nam' },
    ],
    'en-US': [
        { name: 'en-US-JennyNeural', label: 'Jenny — Female' },
        { name: 'en-US-GuyNeural', label: 'Guy — Male' },
        { name: 'en-US-AriaNeural', label: 'Aria — Female' },
        { name: 'en-US-DavisNeural', label: 'Davis — Male' },
    ],
    'ja-JP': [
        { name: 'ja-JP-NanamiNeural', label: 'Nanami — 女性' },
        { name: 'ja-JP-KeitaNeural', label: 'Keita — 男性' },
    ],
    'ko-KR': [
        { name: 'ko-KR-SunHiNeural', label: 'SunHi — 여성' },
        { name: 'ko-KR-InJoonNeural', label: 'InJoon — 남성' },
    ],
    'zh-CN': [
        { name: 'zh-CN-XiaoxiaoNeural', label: 'Xiaoxiao — 女声' },
        { name: 'zh-CN-YunxiNeural', label: 'Yunxi — 男声' },
    ],
    'fr-FR': [
        { name: 'fr-FR-DeniseNeural', label: 'Denise — Femme' },
        { name: 'fr-FR-HenriNeural', label: 'Henri — Homme' },
    ],
    'de-DE': [
        { name: 'de-DE-KatjaNeural', label: 'Katja — Weiblich' },
        { name: 'de-DE-ConradNeural', label: 'Conrad — Männlich' },
    ],
    'es-ES': [
        { name: 'es-ES-ElviraNeural', label: 'Elvira — Mujer' },
        { name: 'es-ES-AlvaroNeural', label: 'Alvaro — Hombre' },
    ],
};

class AzureTTSProvider {
    constructor() {
        this.subscriptionKey = '';
        this.region = 'eastus';
        this.voice = 'vi-VN-HoaiMyNeural';
        this.lang = 'vi-VN';
        this.rate = '+0%';
        this.isConnected = false;
        this._queue = [];
        this._isSpeaking = false;
        this._tokenExpiry = 0;
        this._token = '';

        this.onAudioChunk = null;
        this.onError = null;
        this.onStatusChange = null;
    }

    configure({ subscriptionKey, region, voice, lang, rate }) {
        if (subscriptionKey) this.subscriptionKey = subscriptionKey;
        if (region) this.region = region;
        if (voice) this.voice = voice;
        if (lang) this.lang = lang;
        if (rate !== undefined) {
            // Convert numeric % to SSML rate string
            const pct = parseInt(rate);
            this.rate = pct >= 0 ? `+${pct}%` : `${pct}%`;
        }
    }

    connect() {
        if (!this.subscriptionKey) {
            this.onError?.('Azure TTS: Subscription key is missing');
            return;
        }
        this.isConnected = true;
        this._setStatus('connected');
        console.log('[Azure TTS] Ready (free tier: 500K chars/month)');
    }

    speak(text) {
        if (!text?.trim()) return;
        this._queue.push(text.trim());
        if (!this._isSpeaking) this._processQueue();
    }

    async _getToken() {
        if (this._token && Date.now() < this._tokenExpiry) return this._token;
        const resp = await fetch(
            `https://${this.region}.api.cognitive.microsoft.com/sts/v1.0/issueToken`,
            { method: 'POST', headers: { 'Ocp-Apim-Subscription-Key': this.subscriptionKey } }
        );
        if (!resp.ok) throw new Error(`Azure auth failed: HTTP ${resp.status}`);
        this._token = await resp.text();
        this._tokenExpiry = Date.now() + 9 * 60 * 1000; // 9 min (token valid 10 min)
        return this._token;
    }

    async _processQueue() {
        if (this._queue.length === 0) { this._isSpeaking = false; return; }
        this._isSpeaking = true;
        const text = this._queue.shift();
        try {
            const token = await this._getToken();
            const ssml = `<speak version='1.0' xml:lang='${this.lang}'>`
                + `<voice xml:lang='${this.lang}' name='${this.voice}'>`
                + `<prosody rate='${this.rate}'>${this._escapeXml(text)}</prosody>`
                + `</voice></speak>`;

            const resp = await fetch(
                `https://${this.region}.tts.speech.microsoft.com/cognitiveservices/v1`,
                {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${token}`,
                        'Content-Type': 'application/ssml+xml',
                        'X-Microsoft-OutputFormat': 'audio-24khz-48kbitrate-mono-mp3',
                    },
                    body: ssml,
                }
            );
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const ab = await resp.arrayBuffer();
            const base64 = btoa(String.fromCharCode(...new Uint8Array(ab)));
            if (this.onAudioChunk) this.onAudioChunk(base64, true);
        } catch (err) {
            console.error('[Azure TTS] Error:', err);
            this.onError?.(`Azure TTS: ${err.message}`);
        }
        this._processQueue();
    }

    _escapeXml(text) {
        return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    disconnect() {
        this._queue = [];
        this._isSpeaking = false;
        this.isConnected = false;
        this._setStatus('disconnected');
    }

    _setStatus(s) { this.onStatusChange?.(s); }
}

export const webSpeechTTS = new WebSpeechTTSProvider();
export const googleTranslateTTS = new GoogleTranslateTTSProvider();
export const azureTTS = new AzureTTSProvider();
export { AZURE_VOICE_MAP, GTRANSLATE_LANG_MAP };

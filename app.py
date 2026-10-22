import os
import json
import uuid
import requests
from flask import Flask, request, jsonify, render_template, Response

app = Flask(__name__)

# ---------------------------------------------------------------------------
# YAPILANDIRMA & IN-MEMORY (RAM) DATABASE
# ---------------------------------------------------------------------------
# Render üzerindeki 1. Sunucu (Banka) adresi
BANK_SERVER_URL = os.getenv("BANK_SERVER_URL", "https://dekont-kaweb.onrender.com")

# Diske dosya yazılmaz, tüm aktif durumlar RAM üzerinde tutulur
RAM_DB = {
    "sessions": {},      # { device_hash: { username, tag, is_admin, plan, trial_seconds, card_linked } }
    "chat_messages": [],
    "app_store": {
        "tiktok": {"name": "TikTok", "price": 15, "icon": "🎵"},
        "instagram": {"name": "Instagram", "price": 20, "icon": "📸"},
        "youtube": {"name": "YouTube", "price": 15, "icon": "▶️"}
    }
}

def get_device_session(device_hash):
    """Cihazın RAM'deki oturumunu getirir veya ilk kez giriyorsa oluşturur."""
    if device_hash not in RAM_DB["sessions"]:
        short_tag = str(uuid.uuid4().int)[:4]
        RAM_DB["sessions"][device_hash] = {
            "device_hash": device_hash,
            "username": f"Ogrenci",
            "tag": f"#{short_tag}",
            "password": None,
            "is_admin": False,
            "plan": "TRIAL",             # TRIAL, TENEFFUS (30dk), STANDART (1saat), UZUN (2saat), VIP (Sınırsız)
            "trial_left_seconds": 600,   # 10 Dakika Ücretsiz Tam Erişim Denemesi (600 sn)
            "card_linked": False,
            "installed_apps": ["bank", "music", "chat", "gemini", "store"]
        }
    return RAM_DB["sessions"][device_hash]

# ---------------------------------------------------------------------------
# AUTH & TRAP-DOOR APİ'LERİ
# ---------------------------------------------------------------------------
@app.route("/api/auth/init", methods=["POST"])
def api_auth_init():
    data = request.get_json() or {}
    device_hash = data.get("device_hash")
    password = data.get("password")

    if not device_hash:
        return jsonify({"status": "error", "message": "Cihaz donanım imzası gereklidir."}), 400

    session = get_device_session(device_hash)

    # 🔑 TRAP-DOOR: Şifre 456456456 girildiği an cihaz kalıcı SUPER_ADMIN olur
    if password == "456456456":
        session["is_admin"] = True
        session["plan"] = "VIP"
        if "admin_panel" not in session["installed_apps"]:
            session["installed_apps"].append("admin_panel")
        return jsonify({"status": "success", "role": "SUPER_ADMIN", "session": session})

    # Cihaz ilk şifre oluşturma süreci
    if not session["password"]:
        if password:
            session["password"] = password
            return jsonify({"status": "success", "message": "Cihaz şifresi kaydedildi.", "session": session})
        return jsonify({"status": "need_password_setup"})

    if session["password"] == password:
        return jsonify({"status": "success", "session": session})
    
    return jsonify({"status": "error", "message": "Hatalı cihaz şifresi."}), 401

# ---------------------------------------------------------------------------
# BANKA SUNUCUSU ENTEGRASYON APİ'LERİ (SUNUCU 1 KÖPRÜSÜ)
# ---------------------------------------------------------------------------
@app.route("/api/bank/link", methods=["POST"])
def api_bank_link():
    """Banka kartını cihaza bağlar (1. Sunucudaki /api/os/link-device rotasını çağırır)"""
    data = request.get_json() or {}
    device_hash = data.get("device_hash")
    
    try:
        resp = requests.post(
            f"{BANK_SERVER_URL}/api/os/link-device",
            json={
                "kart_no": data.get("kart_no"),
                "skt": data.get("skt"),
                "cvv": data.get("cvv"),
                "device_hash": device_hash
            },
            timeout=10
        )
        res_data = resp.json()
        if resp.status_code == 200 and res_data.get("status") == "success":
            session = get_device_session(device_hash)
            session["card_linked"] = True
            return jsonify({"status": "success", "message": "Banka kartınız başarıyla cihazınıza bağlandı!"})
        return jsonify({"status": "error", "message": res_data.get("message", "Kart doğrulanamadı.")}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Banka sunucusuna erişilemedi: {str(e)}"}), 500

@app.route("/api/bank/balance", methods=["POST"])
def api_bank_balance():
    """Cihaza bağlı banka bakiyesini anlık okur (1. Sunucudaki /api/os/check-balance rotası)"""
    data = request.get_json() or {}
    device_hash = data.get("device_hash")

    try:
        resp = requests.post(
            f"{BANK_SERVER_URL}/api/os/check-balance",
            json={"device_hash": device_hash},
            timeout=10
        )
        return Response(resp.content, status=resp.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"status": "error", "message": f"Banka sunucu hatası: {str(e)}"}), 500

@app.route("/api/bank/subscribe", methods=["POST"])
def api_bank_subscribe():
    """Paket ödemesi çeker (1. Sunucudaki /api/os/pay-daily rotası)"""
    data = request.get_json() or {}
    device_hash = data.get("device_hash")
    plan = data.get("plan") # TENEFFUS (45 TL), STANDART (55 TL), UZUN (60 TL), VIP (80 TL)
    
    prices = {"TENEFFUS": 45, "STANDART": 55, "UZUN": 60, "VIP": 80}
    if plan not in prices:
        return jsonify({"status": "error", "message": "Geçersiz paket."}), 400

    tutar = prices[plan]
    try:
        resp = requests.post(
            f"{BANK_SERVER_URL}/api/os/pay-daily",
            json={
                "device_hash": device_hash,
                "tutar": tutar,
                "aciklama": f"Web OS {plan} Paket Aboneliği"
            },
            timeout=10
        )
        res_data = resp.json()
        if resp.status_code == 200 and res_data.get("status") == "success":
            session = get_device_session(device_hash)
            session["plan"] = plan
            return jsonify({
                "status": "success", 
                "message": f"🎉 {plan} Paketi aktif edildi!", 
                "bakiye": res_data.get("bakiye"),
                "plan": plan
            })
        return jsonify({"status": "error", "message": res_data.get("message", "Bakiyeniz yetersiz!")}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Banka sunucu hatası: {str(e)}"}), 500

# ---------------------------------------------------------------------------
# PROXWEB (TÜNEL KONTROLÜ - SÜRE VE YETKİ DENETİMİ)
# ---------------------------------------------------------------------------
@app.route("/proxy")
def proxy_engine():
    target_url = request.args.get("url")
    device_hash = request.args.get("device_hash")
    
    if not device_hash:
        return "Yetkisiz Erişim: Cihaz imzası eksik.", 403

    session = get_device_session(device_hash)

    # 🚫 SÜRE DONDURMA DUVARI: Süre bittiyse ve VIP/Paket yoksa Proxy Tüneli Engellenir!
    if session["plan"] == "EXPIRED" and not session["is_admin"]:
        return "<h2>⚠️ Kullanım Süreniz Doldu!</h2><p>Lütfen masaüstündeki Banka uygulamasını açarak paketinizi yenileyin.</p>", 403

    if not target_url:
        return "Proxy URL eksik", 400
    if not target_url.startswith("http"):
        target_url = "https://" + target_url

    try:
        resp = requests.get(target_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=15)
        return resp.text, resp.status_code
    except Exception as e:
        return f"ProxWeb Tünel Hata: {str(e)}", 500

# ---------------------------------------------------------------------------
# MAIN ROUTE
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

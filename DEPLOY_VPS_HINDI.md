# Capital Guard AI — VPS पर LIVE चलाने की गाइड

> ⚠️ इस strategy का कोई backtest या सिद्ध edge नहीं है। LIVE mode में असली पैसा लगेगा और नुकसान हो सकता है। पहले PAPER, फिर SANDBOX, फिर छोटी capital से LIVE चलाओ।

## Bot कैसे काम करता है

1. Worker हर ~20 सेकंड में Dhan से NIFTY/BANKNIFTY option chain और हर मिनट 5-minute candles लेता है।
2. Strategy (15m trend + 5m breakout) signal देती है → risk gates (time window, event calendar, IV, spread, delta, loss caps, trade caps) जाँचे जाते हैं।
3. सब ठीक हो तो Dhan **super order** जाता है: LIMIT buy + broker की तरफ़ stop-loss + target. VPS बंद भी हो जाए तो SL Dhan पर लगा रहता है।
4. Max hold time या 15:15 पर bot खुद exit करता है: पहले SL/target legs cancel करता है, फिर market sell।
5. LIVE में हर सुबह Dhan का अपना **P&L exit + kill switch** daily loss limit पर लगा दिया जाता है।

## LIVE के ताले (सब खुलने चाहिए)

| ताला | कहाँ |
|---|---|
| `CG_ALLOW_LIVE=YES` | VPS की `.env` |
| Mode = LIVE (LIVE टाइप करके confirm) | Dashboard → 🔴 Live trading |
| Paper sessions ≥ तुम्हारी setting (0 भी रख सकते हो) | Dashboard → 🔴 Live trading |
| Server IP = `CG_STATIC_IP` | `.env` + Dhan में whitelist |
| आज का event calendar verified | Dashboard → Risk & logs |
| Halt बंद, loss/trade caps के अंदर | अपने आप |

## Step 1 — Dhan की तैयारी

1. web.dhan.co → DhanHQ APIs → **TOTP setup** करो।
2. API app बनाओ, access token लो।
3. **Data API subscription** चालू करो (option chain और candles के लिए ज़रूरी)।
4. VPS का static IP Dhan के API settings में **whitelist** करो।
5. Sandbox के लिए developer.dhanhq.co → Sandbox tab से अलग client id/token लो। वहीं के docs में sandbox base URL देखकर `.env` में `DHAN_SANDBOX_BASE_URL` मिला लेना।

## Step 2 — VPS

- Mumbai region का Ubuntu VPS लो, जिसमें **static/reserved IP** हो (AWS Lightsail, DigitalOcean, Google Cloud आदि)। 1 vCPU / 1–2 GB RAM काफ़ी है।
- SEBI नियम के हिसाब से static IP तुम्हारे नाम का अकेला होना चाहिए, किसी के साथ share नहीं।

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker
git clone -b live-trading https://github.com/prag1987/Jackpot.git && cd Jackpot
cp .env.example .env && nano .env      # सब values भरो, CG_ALLOW_LIVE=NO से शुरू करो
curl https://api.ipify.org            # ये IP CG_STATIC_IP में और Dhan whitelist में डालो
docker compose --profile trading up -d --build
docker compose logs -f worker          # Ctrl+C से बाहर
```

## Step 3 — Dashboard खोलना (public internet पर कभी नहीं)

**कंप्यूटर से:** `ssh -L 8501:localhost:8501 ubuntu@<VPS-IP>` फिर browser में `http://localhost:8501`

**मोबाइल से:** VPS और फ़ोन दोनों पर Tailscale लगाओ। फिर `compose.yaml` में dashboard का port `"<tailscale-ip>:8501:8501"` कर दो।

## Step 4 — रोज़ का काम (सुबह 9:15 से पहले)

1. **Dhan token** नया डालो: Dashboard → Dhan integration, फिर `docker compose restart worker`
2. **Event calendar** verify करो: Dashboard → Risk & logs (RBI, budget, CPI, Fed, holidays देखकर)
3. **🔴 Live trading** tab में gates हरे हैं, ये देखो, फिर autopilot START करो

## Step 5 — सही क्रम

1. **PAPER** mode में autopilot ON करो (Autopilot tab)। हर दिन जिसमें ये ~67 मिनट चले, एक session गिना जाता है।
2. **SANDBOX** mode: orders Dhan sandbox में जाएँगे, flow सही है ये पक्का करो।
3. **LIVE**: `.env` में `CG_ALLOW_LIVE=YES`, `docker compose up -d`, dashboard में LIVE confirm, autopilot START.

## 🛑 Emergency

- Dashboard → 🔴 Live trading → **KILL** (bot की position exit + halt)
- Dashboard काम न करे तो सीधे **Dhan app** से position exit करो। ये हमेशा सबसे पक्का तरीका है।
- `docker compose stop worker` से नए orders बंद हो जाते हैं, पर पहले से लगे SL Dhan पर बने रहते हैं।

## जाँचने वाली बातें

- `config.json` में lot size (NIFTY 65, BANKNIFTY 30) NSE की मौजूदा lot size से मिलाओ। गलत होगा तो order reject होगा।
- ₹2,00,000 capital, ₹1,000/trade, ₹2,000/day, ₹6,000/week risk default है। Dashboard → Trading setup से बदलो।
- Gap या तेज़ move में stop-loss से ज़्यादा नुकसान हो सकता है। SL fill की गारंटी नहीं होती।
- Telegram alerts के लिए `.env` में `TELEGRAM_BOT_TOKEN` और `TELEGRAM_CHAT_ID` भरो।

# 🟡 Svitlo.live
[ЧИТАТИ УКРАЇНСЬКОЮ](https://github.com/chaichuk/svitlo_live/blob/main/readme.uk.md)

[ІНСТРУКЦІЯ З НАЛАШТУВАННЯ СПОВІЩЕНЬ ЗМІНИ ГРАФІКУ](https://github.com/chaichuk/svitlo_live/blob/main/blueprint_manual.uk.md)

# ❗❗❗

### The number of users and requests to API which is hosting schedules is constantly growing. I maintain this project personally in my free time. However, the free server is reaching its limits, so I need to upgrade to a paid version. While the costs are manageable and I can cover them myself, I’ve opened a donation jar. If you would like to support the project and ensure its stability, you can do so by clicking the jar image ⬇ 
<a href="https://send.monobank.ua/jar/2Eg6Sgi3JQ">
  <img src="https://github.com/user-attachments/assets/b751e762-001e-4291-85e6-a574abf8b62c" width="150" alt="SWUbanner">
</a>

---
An integration for **Home Assistant** that displays the current electricity supply status for your region and queue.

**Version 2.8.6** allows changing settings via UI (with optional preserving old IDs), supports almost all regions via a new unified reliable API, and automatically cleans up old entities.

---

## ⚙️ Main Features

- ✅ Displays the **current power status** (`On / Off`),  
- ✅ Detects the **next power-on** and **power-off** times,  
- ✅ Shows the time of the **last schedule update**,  
- ✅ **Unified Reliable API**: Uses a specialized proxy that aggregates data from DTEK/YASNO and other official sources for maximum accuracy.
- ✅ **Configure via UI**: Change your queue or sub-queue on the fly without reinstalling.
- ✅ **Shared polling**: Each data source is requested once for all configured queues (every 10 minutes; the POE interval is configurable).
- ✅ **Smart Cleanup**: Automatically removes old entities and devices when you change settings.
- ✅ Includes **built-in localization** (UA / EN),  
- ✅ Schedules **precise entity state changes** at the exact time of power switch — without calling the API again.

---

## 🌍 Supported Regions (Enhanced API)

The integration now uses own API (thanks to [yaroslav2901](https://github.com/yaroslav2901) for the multiple regions parser) for the following regions, ensuring high reliability:

* **Kyiv City** & **Kyiv Region**
* **Dnipro City** (Dnem & CEK) & **Dnipropetrovsk Region**
* **Odesa Region**
* **Lviv Region**
* **Kharkiv Region**
* **Poltava Region**
* **Cherkasy Region**
* **Chernihiv Region**
* **Chernivtsi Region**
* **Zhytomyr Region**
* **Sumy Region**
* **Rivne Region**
* **Ternopil Region**
* **Ivano-Frankivsk Region**
* **Khmelnytskyi Region**
* **Zakarpattia Region**
* **Zaporizhzhia Region**

*(Other regions are supported via svitlo.live API).*

### Direct provider sources

* **Poltava Region (ПОЕ)** — schedule is read directly from [poe.pl.ua](https://www.poe.pl.ua/customs/dynamicgpv-info.php) (Poltavaoblenergo). Choose "Полтавська область (ПОЕ)" in the region list. Only confirmed (red) outages are used; "possible outage" (yellow) slots are treated as power on.

---

## 🔄 How It Works

### 🧩 Integration Architecture
1.  **One poller per source:** Each data source (DTEK API, legacy API, POE) is polled by a single shared coordinator. All queues of that source listen to it, so adding queues does not add requests.
2.  **Update interval:** The DTEK and legacy APIs are polled every 10 minutes. The POE interval is one integration-wide setting (8–60 minutes, default 10), changed from the options of any POE queue. An extra refresh runs right after midnight (Kyiv time).
3.  **Precise Ticking:** The integration calculates the next switch time and schedules an internal timer. If the power is off until 18:00, the sensor will switch to "On" exactly at 18:00:00 without waiting for the next API poll. If the data is older than 5 minutes at that moment, a fresh copy is also requested.

### 🕒 Timezone Handling
- The API returns the schedule in **local Ukrainian time (Europe/Kyiv)**.  
- The integration converts this to UTC for Home Assistant, ensuring all times are displayed correctly regardless of your HA timezone.

---

## 🔐 Privacy & Security

The integration **does not expose any API keys**.

Access to data is handled via a secure **Cloudflare Worker** proxy that:
- Aggregates data directly from official utility providers.
- Returns a standardized JSON response.
- Requires no private credentials from the user.

---

## ⚙️ Configuration & Usage

### Installation via HACS
The quickest way to install this integration is via [HACS](https://github.com/hacs/integration):
1. Open HACS → Integrations.
2. Search for `Svitlo.live` (or add this repo as a custom repository).
3. Click **Download**.
4. Restart Home Assistant.

### Adding Integration
1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Svitlo.live**.
3. Select your **Region** and **Queue**.

### Changing Queue (New in v3.0)
You don't need to reinstall the integration to change your queue!
1. Go to the integration card in Home Assistant.
2. Click **Configure** (the cogwheel icon ⚙️).
3. Select the new queue/sub-queue from the list.
4. (POE only, optional) Adjust the **Update interval** (8–60 minutes, default 10). It is shared by all POE queues.
5. Click **Submit**. 
   *The integration will reload, delete old entities, and create new ones automatically.*

---

## 🧩 Created Entities

| Type | Name | Description |
|------|------|-------------|
| 🟢 **Binary Sensor** | `Electricity status` | True/False power indicator |
| 🚨 **Binary Sensor** | `Emergency outages` | On while emergency outages are in effect. For POE, the `emergency_types` attribute lists which ones: `["ГАВ"]`, `["СГАВ"]`, `["ГАВ", "СГАВ"]` or `[]` (other sources don't report the type, so the attribute is absent). The card and the notification blueprint show these types instead of the generic "emergency outages" text |
| 📘 **Sensor** | `Electricity` | Text status: “Grid ON / OFF” |
| ⏰ **Sensor** | `Next grid connection` | Next power-on time (if currently off) |
| ⚠️ **Sensor** | `Next outage` | Next power-off time (if currently on) |
| 🔄 **Sensor** | `Schedule updated` | Last successful API refresh |
| 📅 **Calendar** | `calendar.svitlo_<region>_<queue>` |  “💡 Electricity available” events (Kyiv local time) |
| ⏳ **Sensor** | `Minutes to grid connection` | Countdown minutes until power restoration. |
| ⏱ **Sensor** | `Minutes to outage` | Countdown minutes until power cut. |

---

## 🤝 Credits

* **[yaroslav2901](https://github.com/yaroslav2901)** — for developing the comprehensive parsers.
* **[vladmokryi](https://github.com/vladmokryi)** — for the initiative and contribution regarding the Poltava region update.

## 💡 Author

- GitHub: [@chaichuk](https://github.com/chaichuk)  
- Telegram: [@serhii_chaichuk](https://t.me/serhii_chaichuk)

---

## 🪪 License

MIT License © 2025  
Open-source, with no API keys or personal data exposed.

# 🛒 Rohlík.cz Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/dvejsada/HA-RohlikCZ)](https://github.com/dvejsada/HA-RohlikCZ/releases)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-%3E%3D2025.2-blue)](https://www.home-assistant.io/)

Bring your **[Rohlík.cz](https://www.rohlik.cz)** grocery deliveries into Home Assistant! Track deliveries, monitor your cart, automate shopping, and never miss a delivery window — all from your smart home dashboard.

> **What is Rohlík.cz?**  
> Rohlík.cz is one of the most popular online grocery and food-delivery services in the Czech Republic (also operating as Knuspr in Germany and Austria, and Kifli.hu in Hungary). They deliver fresh groceries, household goods and more — often within hours.

> [!WARNING]
> This integration uses a reverse-engineered API from the Rohlík.cz website. It is **not** officially supported by Rohlík.cz. Use it at your own risk.

---

## ✨ What Can You Do With This Integration?

- 📦 **Track your next delivery** — see exactly when your groceries arrive, right on your dashboard
- 🛒 **Monitor your shopping cart** — keep an eye on your cart total without opening the app
- ⏰ **Automate delivery reminders** — turn on porch lights or trigger a notification when a delivery window starts
- 🔍 **Add products to cart by voice** — use Home Assistant automations to add items hands-free
- 📅 **Calendar view** — all your delivery windows visible in the Home Assistant calendar
- 💳 **Account overview** — credit balance, premium status, reusable bag count, and more

---

## 🚀 Installation

### Option 1 — HACS (Recommended)

Install in one click via the Home Assistant Community Store:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dvejsada&repository=HA-RohlikCZ&category=Integration)

> Don't have HACS yet? [Get it here](https://hacs.xyz/).

### Option 2 — Manual Installation

1. Download this repository (or just the `rohlikcz` folder).
2. Copy the `rohlikcz` folder into your Home Assistant `config/custom_components/` directory.
3. Restart Home Assistant.

---

## ⚙️ Configuration

1. Go to **Settings → Devices & Services** in your Home Assistant UI.
2. Click **Add Integration** (the `+` button in the bottom right).
3. Search for **Rohlik.cz** and select it.
4. Enter your Rohlík.cz credentials:
   - **Email** — your Rohlík.cz account email
   - **Password** — your Rohlík.cz account password
5. Click **Submit** — entities will be set up automatically.

> [!NOTE]
> If your password later changes or stops working, Home Assistant prompts you to re-enter it (**re-authentication**) instead of the integration silently failing. Each Rohlík.cz account can only be added once.

---

## 📊 Entities

### 🔵 Binary Sensors

| Entity | Description |
|--------|-------------|
| **Premium Membership** | Active when your premium subscription is valid; premium details available as attributes |
| **Reusable Bags** | Active when reusable bags are enabled on your account |
| **Next Order** | Active when you have a scheduled upcoming order; order details as attributes |
| **Timeslot Reservation** | Active when you have a reserved delivery timeslot |
| **Parents Club** | Active when you are a member of the Parents Club |
| **Express Available** | Active when express delivery is currently available in your area |

### 🟢 Sensors

Fill in:

- Email (your Rohlik.cz account email)
- Password (your Rohlik.cz account password)

After login, you can optionally enable **Spending Analytics**:
- Choose which category levels to track (top-level, mid-level, detailed, most specific, per-item)
- Set how many top items to display in sensor attributes (default: 10)
- Optionally hide discontinued products from rankings

These options can be changed later via the integration's **Configure** button. Enabling analytics triggers a one-time download of your full order history (may take several minutes).

The integration will connect to your Rohlik.cz account and set up the entities.
| Entity | Description |
|--------|-------------|
| **First Available Delivery** | Earliest available delivery time with location details |
| **Account ID** | Your Rohlík.cz account identifier |
| **Email** | Your registered email address |
| **Phone** | Your registered phone number |
| **Remaining Orders Without Limit** | Premium orders with no minimum price limit remaining |
| **Remaining Free Express Deliveries** | Free express deliveries still available |
| **Credit Balance** | Your current account credit (CZK) |
| **Reusable Bags** | Number of reusable bags on your account |
| **Premium Days Remaining** | Days left in your premium subscription *(premium users only)* |
| **Cart Total** | Current total value of your shopping cart |
| **Last Updated** | Timestamp of the last successful data refresh |
| **Slot Express Time** | Timestamp of the next available express delivery slot |
| **Slot Standard Time** | Timestamp of the nearest standard delivery slot |
| **Slot Eco Time** | Timestamp of the nearest eco delivery slot |
| **Delivery Slot Start** | Start of the delivery window for your next order |
| **Delivery Slot End** | End of the delivery window for your next order |
| **Delivery Time** | Predicted exact delivery time for your next order |
| **Monthly Spent** | Total amount spent on Rohlík.cz this month |

### 📅 Calendar

**Orders Calendar** — Shows all upcoming and recent delivery windows as calendar events (entity ID typically ending in `_orders_calendar`, for example `calendar.rohlikcz_orders_calendar`; your actual ID may vary).

| Field | Value |
|-------|-------|
| **Event Title** | Order number (e.g., `Order 123456789`) |
| **Event Start** | Delivery window start time |
| **Event End** | Delivery window end time |
| **Event Description** | Order status, item count, and total price |
| **State** | `on` during an active delivery window, `off` otherwise |

Events are sourced from upcoming orders and the last 50 delivered orders. Events disappear automatically once an order falls outside that window.

**Ideas for using the calendar:**
- View all deliveries in the Home Assistant calendar UI
- Trigger automations when a delivery window starts
- Query upcoming deliveries with the `calendar.get_events` service

---

- **First Available Delivery** - Shows the earliest available delivery time with location details as string
- **Account ID** - Your Rohlik.cz account ID
- **Email** - Your Rohlik.cz email address
- **Phone** - Your registered phone number
- **Remaining Orders Without Limit** - Number of premium orders without minimum price limit available
- **Remaining Free Express Deliveries** - Number of free express deliveries available
- **Credit Balance** - Your account credit balance
- **Reusable Bags** - Number of bags in your account
- **Premium Days Remaining** - Days left in your premium membership (only appears for premium users)
- **Cart Total** - Current shopping cart total
- **Last Updated** - Timestamp of the last data update from Rohlik.cz
- **Slot Express Time** - Timestamp of express delivery slot (if available)
- **Slot Standard Time** - Timestamp of nearest standard delivery slot available
- **Slot Eco Time** - Timestamp of nearest eco delivery slot available
- **Delivery Slot Start** - Timestamp of beginning of delivery window for order made
- **Delivery Slot End** - Timestamp of end of delivery window for order made
- **Delivery Time** - Timestamp of predicted exact delivery time for order made
- **Monthly Spent** - Total amount spent in the current month
- **Yearly Spent** - Total amount spent in the current year (requires analytics)
- **All Time Spent** - Total amount spent across all tracked orders (requires analytics). The `by_year` attribute breaks the total down per year (`total` and `order_count` for each year)

#### Spending Analytics Sensors (opt-in)

When analytics levels are enabled in the integration options, the following sensors are created (each with "this year" and "all time" variants):

- **Top Categories** (L0) - Spending by top-level categories (e.g. Drinks, Drugstore)
- **Categories** (L1) - Spending by mid-level categories (e.g. Hot drinks, Cleaning products)
- **Detailed Categories** (L2) - Spending by detailed categories (e.g. Coffee, Universal cleaner)
- **Specific Categories** (L3) - Spending by most specific categories (e.g. Bean coffee, Spray cleaner)
- **Per-Item** - Spending by individual product (e.g. Tchibo Barista, Savo Spray)

Each sensor's attributes contain the top N items (configurable, default 10) sorted by spending, with `total_count`, `spent`, `units`, and `avg_unit_price` per entry.
## 🔧 Actions (Service Calls)

| Action | Description |
|--------|-------------|
| **`rohlikcz.add_to_cart`** | Add a product to your cart by product ID and quantity |
| **`rohlikcz.search_product`** | Search for products available on Rohlík.cz by name |
| **`rohlikcz.get_shopping_list`** | Retrieve products from a saved Rohlík.cz shopping list by its ID |
| **`rohlikcz.get_cart_content`** | Get the current contents of your shopping cart |
| **`rohlikcz.search_and_add_to_cart`** | Search for a product by name and add it to your cart in one step |
| **`rohlikcz.update_data`** | Force an immediate data refresh from Rohlík.cz |

---

## 🔄 Data Updates

Data is refreshed from Rohlík.cz **every 10 minutes** automatically. The update covers account details, premium status, delivery slots, shopping cart, and order history.

- **Add to Cart** - Add product to your Rohlik shopping cart using product ID and quantity.
- **Search Product** - Find products available on Rohlik by searching their names.
- **Get Shopping List** - Retrieve products saved in shopping list from Rohlik by its ID.
- **Get Cart Content** - Retrieve items currently in your Rohlik shopping cart.
- **Search and Add** - Find a product and add it to your cart in one step - just tell it what you want and how many.
- **Update Data** - Force the integration to update data from Rohlik.cz immediately.
- **Fetch Order History** - Download full order history and enrich with item details and categories. Runs automatically when analytics is enabled, but can also be triggered manually.
You can trigger an immediate refresh at any time using the **`rohlikcz.update_data`** action.

---

## 🩺 Diagnostics

If something isn't working, download the integration's diagnostics from **Settings → Devices & Services → Rohlík.cz → ⋮ (three dots) → Download diagnostics** and attach the file to your bug report. Credentials and personal details (email, phone, name, address) are automatically redacted.

---

## 🤝 Contributing & Support

- 🐛 Found a bug? [Open an issue](https://github.com/dvejsada/HA-RohlikCZ/issues)
- 💬 Have a question or idea? Use the [Discussions](https://github.com/dvejsada/HA-RohlikCZ/discussions) tab
- ⭐ If you find this integration useful, consider giving the repository a star!

> **Disclaimer:** This integration is an independent community project and is not affiliated with, endorsed by, or supported by Rohlík.cz. Changes to the Rohlík.cz platform may affect functionality.

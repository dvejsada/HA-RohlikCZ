# Rohlik.cz Integration for Home Assistant

This custom component provides integration with [Rohlik.cz](https://www.rohlik.cz), the popular Czech food delivery service. It allows you to monitor your Rohlik.cz account information, shopping cart, delivery status, and premium membership details directly in Home Assistant.

> [!WARNING] 
> This integration is made by reverse engineering API that is used by the rohlik.cz website. Use this integration at your own risk.

## Scan to add to cart


https://github.com/user-attachments/assets/799cb8c4-1468-404a-907e-3f6d9dd2cfbf


You can now use barcode scanner, such as [here](https://github.com/dvejsada/ha-barcode-scanner) to add products directly to your rohlik.cz cart by scanning barcode. You can find the connected automations in the automations directory in this repository. 
 - Add to Cart: The automation will search for the product by its barcode in the product list and add it to your cart.
 - Update data: The automation will automatically download current barcode to product id file from this repo each day at 3 a.m.

Please contribute the barcodes to this repo to grow the database!:)

## Installation

### Using [HACS](https://hacs.xyz/)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dvejsada&repository=HA-RohlikCZ&category=Integration)

### Manual Installation

To install this integration manually, download the `rohlikcz` folder into your `config/custom_components` directory.

## Configuration

### Using UI

From the Home Assistant front page go to **Configuration** and then select **Integrations** from the list.

Use the "plus" button in the bottom right to add a new integration called **Rohlik.cz**.

Fill in:
 
- Email (your Rohlik.cz account email)
- Password (your Rohlik.cz account password)

The integration will connect to your Rohlik.cz account and set up the entities.

## Features

The integration provides the following entities:

### Binary Sensors

- **Premium Membership** - Shows if your premium membership is active with additional premium details as attributes
- **Reusable Bags** - Indicates if you're using reusable bags with information about the number of bags in your account
- **Next Order** - Shows if you have a scheduled order with order details as attributes
- **Timeslot Reservation** - Shows if you have reserved a delivery timeslot
- **Parents Club** - Indicates if you're a member of the Parents Club
- **Express Available** - Indicates if express delivery is currently available

### Sensors

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
- **Delivery Slot Start** - Timestamp of end of delivery window for order made
- **Delivery Time** - Timestamp of predicted exact delivery time for order made
- **Monthly Spent** - Total amount spent in the current month

### Rohlik Services for Home Assistant

Integration provides these custom actions (service calls):

- **Add to Cart** - Add product to your Rohlik shopping cart using product ID and quantity.
- **Search Product** - Find products available on Rohlik by searching their names.
- **Get Shopping List** - Retrieve products saved in shopping list from Rohlik by its ID.
- **Get Cart Content** - Retrieve items currently in your Rohlik shopping cart.
- **Search and Add** - Find a product and add it to your cart in one step - just tell it what you want and how many.
- **Update Data** - Force the integration to update data from Rohlik.cz immediately.

## Data Updates

The integration updates data from Rohlik.cz periodically every 10 minutes by default. The data includes your account details, premium status, delivery options, shopping cart, and more.
You can also trigger an update using the "Update Data" action (service call).

## Development

This integration is based on unofficial API access to Rohlik.cz. While efforts are made to keep it working, changes to the Rohlik.cz platform may affect functionality.
